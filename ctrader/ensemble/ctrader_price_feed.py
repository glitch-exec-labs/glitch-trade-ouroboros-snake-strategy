"""
GlitchExecutor Ensemble - cTrader Price Feed

Fetches OHLCV bar data from the cTrader Open API using the admin's live account.
Provides the same interface as MT5PriceFeed and IBPriceFeed so the ensemble
engine can use it as a drop-in replacement for forex/commodity/stock data.

Credentials come from environment variables (system-level admin account):
  CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID
"""
import asyncio
import concurrent.futures
import logging
import os
import ssl
import struct
import time
import uuid
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger("CTraderPriceFeed")

LIVE_HOST = "live.ctraderapi.com"
DEMO_HOST = "demo.ctraderapi.com"
PORT = 5035

PT_APP_AUTH_REQ  = 2100
PT_APP_AUTH_RES  = 2101
PT_ACCOUNT_AUTH_REQ = 2102
PT_ACCOUNT_AUTH_RES = 2103
PT_SYMBOLS_LIST_REQ = 2114
PT_SYMBOLS_LIST_RES = 2115
PT_TRENDBARS_REQ = 2137
PT_TRENDBARS_RES = 2138
PT_ERROR_RES     = 2142
PT_HEARTBEAT     = 51

_TF_MAP = {"m15": 7, "15m": 7, "h1": 9, "1h": 9, "h4": 10, "4h": 10}
_FETCH_COUNTS = {"m15": 300, "h1": 200, "h4": 200}

_SYMBOL_CACHE: Dict[str, Tuple[int, int]] = {}
_SYMBOL_CACHE_TS: float = 0.0
_SYMBOL_CACHE_TTL = 3600

try:
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq,
        ProtoOASymbolsListReq, ProtoOASymbolsListRes,
        ProtoOAGetTrendbarsReq, ProtoOAGetTrendbarsRes,
        ProtoOAErrorRes,
    )
    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False
    logger.warning("ctrader-open-api not installed — run: pip install ctrader-open-api")


def _run_async(coro, timeout: int = 30):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=timeout)


class CTraderPriceFeed:
    """
    cTrader OHLCV price feed for the GlitchExecutor ensemble engine.
    Interface is identical to MT5PriceFeed and IBPriceFeed.
    """

    def __init__(self):
        if not PROTO_AVAILABLE:
            logger.error("ctrader-open-api not installed — cTrader price feed disabled")

        self.client_id     = os.environ.get("CTRADER_CLIENT_ID", "")
        self.client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
        self.access_token  = os.environ.get("CTRADER_ACCESS_TOKEN", "")
        self.account_id    = int(os.environ.get("CTRADER_ACCOUNT_ID", "0") or 0)
        self.host = DEMO_HOST if os.environ.get("CTRADER_LIVE", "true").lower() == "false" else LIVE_HOST

        if self.client_id and self.account_id:
            logger.info(f"CTraderPriceFeed ready — account {self.account_id} via {self.host}:{PORT}")
        else:
            logger.warning("CTraderPriceFeed: set CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN/ACCOUNT_ID")

    def _frame(self, payload_type: int, payload_bytes: bytes, mid: str = None) -> bytes:
        msg = ProtoMessage()
        msg.payloadType = payload_type
        msg.payload = payload_bytes
        if mid:
            msg.clientMsgId = mid
        encoded = msg.SerializeToString()
        return struct.pack('>I', len(encoded)) + encoded

    async def _recv(self, reader, timeout: int = 15) -> ProtoMessage:
        hdr = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = struct.unpack('>I', hdr)[0]
        data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        msg = ProtoMessage()
        msg.ParseFromString(data)
        return msg

    async def _recv_until(self, reader, expected: list, mid: str = None, timeout: int = 15):
        while True:
            msg = await self._recv(reader, timeout)
            if msg.payloadType == PT_HEARTBEAT:
                continue
            if msg.payloadType == PT_ERROR_RES:
                err = ProtoOAErrorRes()
                err.ParseFromString(msg.payload)
                raise RuntimeError(f"cTrader error {err.errorCode}: {err.description}")
            if mid and msg.clientMsgId and msg.clientMsgId != mid:
                continue
            if msg.payloadType in expected:
                return msg

    async def _session(self, operation):
        ssl_ctx = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(self.host, PORT, ssl=ssl_ctx)
        try:
            req = ProtoOAApplicationAuthReq()
            req.clientId = self.client_id
            req.clientSecret = self.client_secret
            mid = str(uuid.uuid4())[:8]
            writer.write(self._frame(PT_APP_AUTH_REQ, req.SerializeToString(), mid))
            await writer.drain()
            await self._recv_until(reader, [PT_APP_AUTH_RES], mid)

            req2 = ProtoOAAccountAuthReq()
            req2.ctidTraderAccountId = self.account_id
            req2.accessToken = self.access_token
            mid2 = str(uuid.uuid4())[:8]
            writer.write(self._frame(PT_ACCOUNT_AUTH_REQ, req2.SerializeToString(), mid2))
            await writer.drain()
            await self._recv_until(reader, [PT_ACCOUNT_AUTH_RES], mid2)

            return await operation(reader, writer)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=3)
            except Exception:
                pass

    async def _fetch_symbol_map(self, reader, writer) -> Dict[str, Tuple[int, int]]:
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        req.includeArchivedSymbols = False
        mid = str(uuid.uuid4())[:8]
        writer.write(self._frame(PT_SYMBOLS_LIST_REQ, req.SerializeToString(), mid))
        await writer.drain()
        res_msg = await self._recv_until(reader, [PT_SYMBOLS_LIST_RES], mid)
        res = ProtoOASymbolsListRes()
        res.ParseFromString(res_msg.payload)
        result = {}
        for sym in res.symbol:
            digits = getattr(sym, 'digits', 5) or 5
            result[sym.symbolName.upper()] = (sym.symbolId, digits)
        logger.info(f"Loaded {len(result)} symbols from cTrader broker")
        return result

    def _get_symbol_info(self, name: str) -> Optional[Tuple[int, int]]:
        global _SYMBOL_CACHE, _SYMBOL_CACHE_TS
        now = time.time()
        if not _SYMBOL_CACHE or now - _SYMBOL_CACHE_TS > _SYMBOL_CACHE_TTL:
            try:
                async def fetch(r, w):
                    return await self._fetch_symbol_map(r, w)
                _SYMBOL_CACHE = _run_async(self._session(fetch), timeout=30)
                _SYMBOL_CACHE_TS = now
            except Exception as e:
                logger.error(f"Symbol cache refresh failed: {e}")
                return None
        return _SYMBOL_CACHE.get(name.upper())

    async def _fetch_bars(self, reader, writer, symbol_id: int, digits: int,
                          tf_enum: int, count: int) -> Optional[np.ndarray]:
        now_ms = int(time.time() * 1000)
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = symbol_id
        req.period = tf_enum
        req.toTimestamp = now_ms
        req.fromTimestamp = now_ms - (count * 900000)  # count * 15min in ms (conservative)
        req.count = count
        mid = str(uuid.uuid4())[:8]
        writer.write(self._frame(PT_TRENDBARS_REQ, req.SerializeToString(), mid))
        await writer.drain()
        res_msg = await self._recv_until(reader, [PT_TRENDBARS_RES], mid, timeout=20)
        res = ProtoOAGetTrendbarsRes()
        res.ParseFromString(res_msg.payload)
        if not res.trendbar:
            return None
        divisor = 10.0 ** digits
        rows = []
        for bar in res.trendbar:
            ts    = bar.utcTimestampInMinutes * 60
            low   = bar.low / divisor
            open_ = (bar.low + bar.deltaOpen) / divisor
            close = (bar.low + bar.deltaClose) / divisor
            high  = (bar.low + bar.deltaHigh) / divisor




            vol   = bar.volume if bar.HasField("volume") else 0
            rows.append([ts, open_, high, low, close, float(vol)])
        if not rows:
            return None
        arr = np.array(rows, dtype=float)
        return arr[arr[:, 0].argsort()]

    def get_candles(self, symbol: str, timeframe: str, limit: int) -> Optional[np.ndarray]:
        if not PROTO_AVAILABLE or not self.client_id:
            return None
        tf_enum = _TF_MAP.get(timeframe.lower())
        if tf_enum is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None
        sym_info = self._get_symbol_info(symbol)
        if not sym_info:
            logger.error(f"Symbol '{symbol}' not found on broker account")
            return None
        symbol_id, digits = sym_info
        try:
            async def fetch(r, w):
                return await self._fetch_bars(r, w, symbol_id, digits, tf_enum, limit)
            candles = _run_async(self._session(fetch), timeout=30)
            if candles is not None:
                logger.info(f"[cTrader] {len(candles)} {timeframe} bars for {symbol}")
            return candles
        except Exception as e:
            logger.error(f"[cTrader] get_candles failed for {symbol} {timeframe}: {e}")
            return None

    def get_candles_multi_timeframe(self, symbol: str) -> Dict[str, np.ndarray]:
        if not PROTO_AVAILABLE or not self.client_id:
            return {}
        sym_info = self._get_symbol_info(symbol)
        if not sym_info:
            logger.error(f"[cTrader] Symbol '{symbol}' not found")
            return {}
        symbol_id, digits = sym_info

        async def fetch_all(reader, writer):
            result = {}
            for tf_key, tf_enum in [("m15", 7), ("h1", 9), ("h4", 10)]:
                try:
                    bars = await self._fetch_bars(
                        reader, writer, symbol_id, digits, tf_enum,
                        _FETCH_COUNTS[tf_key]
                    )
                    if bars is not None and len(bars) > 0:
                        result[tf_key] = bars
                except Exception as e:
                    logger.error(f"[cTrader] {tf_key} bars failed for {symbol}: {e}")
            return result

        try:
            result = _run_async(self._session(fetch_all), timeout=45)
            if result:
                logger.info(f"[cTrader] Multi-TF for {symbol}: {[(k, len(v)) for k, v in result.items()]}")
            return result or {}
        except Exception as e:
            logger.error(f"[cTrader] get_candles_multi_timeframe failed for {symbol}: {e}")
            return {}

    def get_current_price(self, symbol: str) -> Optional[float]:
        candles = self.get_candles(symbol, "m15", 1)
        if candles is not None and len(candles) > 0:
            return float(candles[-1, 4])
        return None
