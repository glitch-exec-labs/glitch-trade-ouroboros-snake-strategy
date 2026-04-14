"""
GlitchExecutor - cTrader Open API Client

Async TCP client for the cTrader Open API (live + demo accounts).
Uses asyncio streams directly — no Twisted dependency.
Matches the same interface as MT5Client and ExchangeClient.
"""
import asyncio
import ssl
import struct
import uuid
import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("CTraderClient")

LIVE_HOST = "live.ctraderapi.com"
DEMO_HOST = "demo.ctraderapi.com"
PORT = 5035  # SSL

# Payload type constants (cTrader OpenApiMessages.proto)
PT_APP_AUTH_REQ       = 2100
PT_APP_AUTH_RES       = 2101
PT_ACCOUNT_AUTH_REQ   = 2102
PT_ACCOUNT_AUTH_RES   = 2103
PT_TRADER_REQ         = 2104
PT_TRADER_RES         = 2105
PT_NEW_ORDER_REQ      = 2106
PT_SYMBOLS_LIST_REQ   = 2114
PT_SYMBOLS_LIST_RES   = 2115
PT_RECONCILE_REQ      = 2124
PT_RECONCILE_RES      = 2125
PT_EXECUTION_EVENT    = 2126
PT_CLOSE_POSITION_REQ = 2140
PT_AMEND_SLTP_REQ     = 2141
PT_ERROR_RES          = 2142
PT_HEARTBEAT          = 51

try:
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq,
        ProtoOAAccountAuthReq,
        ProtoOATraderReq, ProtoOATraderRes,
        ProtoOANewOrderReq, ProtoOAExecutionEvent,
        ProtoOASymbolsListReq, ProtoOASymbolsListRes,
        ProtoOAReconcileReq, ProtoOAReconcileRes,
        ProtoOAClosePositionReq,
        ProtoOAAmendPositionSLTPReq,
        ProtoOAErrorRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATradeSide,
    )
    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False
    logger.warning("ctrader-open-api not installed — run: pip install ctrader-open-api")


class CTraderClient:
    """
    Async cTrader Open API client.

    Connects directly to the cTrader TCP API using asyncio streams.
    Each method opens a fresh SSL connection, authenticates, executes
    the operation, then closes — simple and stateless.

    Args:
        client_id:     cTrader app Client ID (from openapi.ctrader.com)
        client_secret: cTrader app Client Secret
        access_token:  OAuth 2.0 access token for the trader account
        account_id:    ctidTraderAccountId (from the account list)
        live:          True for live account, False for demo
    """

    def __init__(self, client_id: str, client_secret: str, access_token: str,
                 account_id: int, live: bool = True):
        if not PROTO_AVAILABLE:
            raise RuntimeError(
                "ctrader-open-api not installed — run: pip install ctrader-open-api"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = int(account_id)
        self.host = LIVE_HOST if live else DEMO_HOST
        self.live = live
        self._timeout = 15

        logger.info(
            f"CTraderClient ready — account {account_id} "
            f"({'LIVE' if live else 'DEMO'}) via {self.host}:{PORT}"
        )

    # ── Low-level TCP helpers ─────────────────────────────────────────────────

    async def _open_connection(self):
        ssl_ctx = ssl.create_default_context()
        return await asyncio.open_connection(self.host, PORT, ssl=ssl_ctx)

    def _build_frame(self, payload_type: int, payload_bytes: bytes,
                     client_msg_id: str = None) -> bytes:
        """Wrap payload in ProtoMessage and prefix with 4-byte big-endian length."""
        msg = ProtoMessage()
        msg.payloadType = payload_type
        msg.payload = payload_bytes
        if client_msg_id:
            msg.clientMsgId = client_msg_id
        encoded = msg.SerializeToString()
        return struct.pack('>I', len(encoded)) + encoded

    async def _recv_frame(self, reader: asyncio.StreamReader) -> ProtoMessage:
        """Read exactly one framed message."""
        length_bytes = await asyncio.wait_for(
            reader.readexactly(4), timeout=self._timeout
        )
        length = struct.unpack('>I', length_bytes)[0]
        data = await asyncio.wait_for(
            reader.readexactly(length), timeout=self._timeout
        )
        msg = ProtoMessage()
        msg.ParseFromString(data)
        return msg

    async def _recv_until(self, reader: asyncio.StreamReader,
                          expected_types: list,
                          client_msg_id: str = None) -> ProtoMessage:
        """
        Read messages until one of expected_types arrives.
        Silently discards heartbeats. Raises on error responses.
        """
        while True:
            msg = await self._recv_frame(reader)

            if msg.payloadType == PT_HEARTBEAT:
                continue

            if msg.payloadType == PT_ERROR_RES:
                err = ProtoOAErrorRes()
                err.ParseFromString(msg.payload)
                raise RuntimeError(
                    f"cTrader API error {err.errorCode}: {err.description}"
                )

            # If we're waiting for a specific msgId, skip unrelated responses
            if (client_msg_id and msg.clientMsgId
                    and msg.clientMsgId != client_msg_id):
                continue

            if msg.payloadType in expected_types:
                return msg

    async def _session(self, operation):
        """
        Open connection → app auth → account auth → run operation → close.
        operation: async callable(reader, writer) -> result
        """
        reader, writer = await self._open_connection()
        try:
            # 1. App authentication
            req = ProtoOAApplicationAuthReq()
            req.clientId = self.client_id
            req.clientSecret = self.client_secret
            mid = str(uuid.uuid4())[:8]
            writer.write(self._build_frame(PT_APP_AUTH_REQ, req.SerializeToString(), mid))
            await writer.drain()
            await self._recv_until(reader, [PT_APP_AUTH_RES], mid)
            logger.debug("App auth OK")

            # 2. Account authentication
            req2 = ProtoOAAccountAuthReq()
            req2.ctidTraderAccountId = self.account_id
            req2.accessToken = self.access_token
            mid2 = str(uuid.uuid4())[:8]
            writer.write(self._build_frame(PT_ACCOUNT_AUTH_REQ, req2.SerializeToString(), mid2))
            await writer.drain()
            await self._recv_until(reader, [PT_ACCOUNT_AUTH_RES], mid2)
            logger.debug(f"Account {self.account_id} auth OK")

            # 3. Caller's operation
            return await operation(reader, writer)

        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=3)
            except Exception:
                pass

    # ── Public interface ──────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Check if cTrader API is reachable (synchronous, for compatibility)."""
        try:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                asyncio.wait_for(self._health_async(), timeout=12)
            )
            loop.close()
            return result
        except Exception as e:
            logger.error(f"cTrader health check failed: {e}")
            return False

    async def _health_async(self) -> bool:
        try:
            await self._session(lambda r, w: asyncio.sleep(0))
            return True
        except Exception as e:
            logger.error(f"cTrader health check error: {e}")
            return False

    async def get_balance(self) -> Dict:
        """Get account balance."""
        try:
            async def fetch(reader, writer):
                req = ProtoOATraderReq()
                req.ctidTraderAccountId = self.account_id
                mid = str(uuid.uuid4())[:8]
                writer.write(self._build_frame(PT_TRADER_REQ, req.SerializeToString(), mid))
                await writer.drain()
                res_msg = await self._recv_until(reader, [PT_TRADER_RES], mid)
                res = ProtoOATraderRes()
                res.ParseFromString(res_msg.payload)

                # balance is in cents (1/100 of deposit currency)
                money_digits = getattr(res.trader, 'moneyDigits', 2) or 2
                divisor = 10 ** money_digits
                balance = res.trader.balance / divisor

                return {
                    "total": round(balance, 2),
                    "free": round(balance, 2),  # conservative: use full balance as free
                    "equity": round(balance, 2),
                    "currency": "USD",
                }

            return await self._session(fetch)

        except Exception as e:
            logger.error(f"get_balance failed: {e}")
            return {"total": 0, "free": 0, "equity": 0, "error": str(e)}

    async def place_order(self, symbol: str, side: str, amount: float,
                          price: float = None, sl: float = None,
                          tp: float = None) -> Dict:
        """
        Place a market (or limit) order.

        Args:
            symbol: Broker symbol name e.g. "EURUSD"
            side:   'buy' or 'sell'
            amount: Volume in lots (e.g. 0.01)
            price:  Limit price — None for market order
            sl:     Stop loss price
            tp:     Take profit price
        """
        try:
            async def send(reader, writer):
                symbol_id = await self._lookup_symbol(reader, writer, symbol)
                if not symbol_id:
                    return {
                        "success": False,
                        "error": f"Symbol '{symbol}' not found on this broker account",
                    }

                req = ProtoOANewOrderReq()
                req.ctidTraderAccountId = self.account_id
                req.symbolId = symbol_id
                req.orderType = (
                    ProtoOAOrderType.Value("MARKET")
                    if price is None
                    else ProtoOAOrderType.Value("LIMIT")
                )
                req.tradeSide = (
                    ProtoOATradeSide.Value("BUY")
                    if side.lower() == "buy"
                    else ProtoOATradeSide.Value("SELL")
                )
                # cTrader volume = centilots (1 lot = 100)
                req.volume = max(1, int(round(amount * 100)))
                if price is not None:
                    req.limitPrice = price
                if sl is not None:
                    req.stopLoss = sl
                if tp is not None:
                    req.takeProfit = tp

                writer.write(self._build_frame(
                    PT_NEW_ORDER_REQ, req.SerializeToString()
                ))
                await writer.drain()

                res_msg = await self._recv_until(reader, [PT_EXECUTION_EVENT])
                ev = ProtoOAExecutionEvent()
                ev.ParseFromString(res_msg.payload)

                order = ev.order
                pos = ev.position if ev.HasField("position") else None
                filled_price = pos.price if pos else (price or 0)

                return {
                    "success": True,
                    "order_id": str(order.orderId),
                    "position_id": str(pos.positionId) if pos else None,
                    "symbol": symbol,
                    "side": side.upper(),
                    "amount": amount,
                    "price": filled_price,
                    "status": "filled",
                    "sl": sl,
                    "tp": tp,
                    "message": f"{side.upper()} {amount} lot(s) {symbol}",
                }

            return await self._session(send)

        except Exception as e:
            logger.error(f"place_order failed: {e}")
            return {"success": False, "error": str(e), "symbol": symbol}

    async def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        try:
            async def fetch(reader, writer):
                return await self._reconcile(reader, writer)

            return await self._session(fetch)

        except Exception as e:
            logger.error(f"get_open_positions failed: {e}")
            return []

    async def close_position(self, symbol: str, position_id: str) -> Dict:
        """
        Close a position by position ID.
        Fetches current volume via reconcile in the same session.
        """
        try:
            async def close(reader, writer):
                # Get current volume so we can pass the right amount
                positions = await self._reconcile(reader, writer)
                volume = None
                for p in positions:
                    if p["ticket"] == str(position_id):
                        # volume stored as lots in our dict; convert back to centilots
                        volume = int(round(p["amount"] * 100))
                        break

                if volume is None:
                    return {
                        "success": False,
                        "error": f"Position {position_id} not found",
                        "ticket": position_id,
                    }

                req = ProtoOAClosePositionReq()
                req.ctidTraderAccountId = self.account_id
                req.positionId = int(position_id)
                req.volume = volume

                writer.write(self._build_frame(
                    PT_CLOSE_POSITION_REQ, req.SerializeToString()
                ))
                await writer.drain()

                await self._recv_until(reader, [PT_EXECUTION_EVENT])

                return {
                    "success": True,
                    "message": f"Closed position #{position_id}",
                    "ticket": position_id,
                }

            return await self._session(close)

        except Exception as e:
            logger.error(f"close_position failed: {e}")
            return {"success": False, "error": str(e), "ticket": position_id}

    async def modify_position(self, position_id: str,
                              sl: float = None, tp: float = None) -> Dict:
        """Modify SL/TP on an open position."""
        try:
            async def modify(reader, writer):
                req = ProtoOAAmendPositionSLTPReq()
                req.ctidTraderAccountId = self.account_id
                req.positionId = int(position_id)
                if sl is not None:
                    req.stopLoss = sl
                if tp is not None:
                    req.takeProfit = tp

                writer.write(self._build_frame(
                    PT_AMEND_SLTP_REQ, req.SerializeToString()
                ))
                await writer.drain()

                await self._recv_until(reader, [PT_EXECUTION_EVENT])

                return {
                    "success": True,
                    "message": f"Modified position #{position_id}",
                    "ticket": position_id,
                    "sl": sl,
                    "tp": tp,
                }

            return await self._session(modify)

        except Exception as e:
            logger.error(f"modify_position failed: {e}")
            return {"success": False, "error": str(e), "ticket": position_id}

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _reconcile(self, reader, writer) -> List[Dict]:
        """Fetch open positions within an existing authenticated session."""
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id
        mid = str(uuid.uuid4())[:8]
        writer.write(self._build_frame(PT_RECONCILE_REQ, req.SerializeToString(), mid))
        await writer.drain()

        res_msg = await self._recv_until(reader, [PT_RECONCILE_RES], mid)
        res = ProtoOAReconcileRes()
        res.ParseFromString(res_msg.payload)

        positions = []
        for pos in res.position:
            td = pos.tradeData
            positions.append({
                "ticket": str(pos.positionId),
                "symbol": str(td.symbolId),  # symbol ID; resolve to name separately if needed
                "side": "BUY" if td.tradeSide == ProtoOATradeSide.Value("BUY") else "SELL",
                "amount": td.volume / 100.0,  # centilots → lots
                "entry_price": pos.price if pos.HasField("price") else 0,
                "sl": pos.stopLoss if pos.HasField("stopLoss") else None,
                "tp": pos.takeProfit if pos.HasField("takeProfit") else None,
                # NOTE: ProtoOAPosition has no unrealizedPnl field; authoritative
                # P&L must be fetched via ProtoOADealListReq after close. Return 0
                # here so callers that only need ticket existence still work.
                "profit": 0,
            })
        return positions

    async def _lookup_symbol(self, reader, writer, symbol_name: str) -> Optional[int]:
        """Look up the numeric symbolId for a symbol name within a session."""
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        req.includeArchivedSymbols = False
        mid = str(uuid.uuid4())[:8]
        writer.write(self._build_frame(PT_SYMBOLS_LIST_REQ, req.SerializeToString(), mid))
        await writer.drain()

        res_msg = await self._recv_until(reader, [PT_SYMBOLS_LIST_RES], mid)
        res = ProtoOASymbolsListRes()
        res.ParseFromString(res_msg.payload)

        for sym in res.symbol:
            if sym.symbolName.upper() == symbol_name.upper():
                return sym.symbolId

        available = [s.symbolName for s in res.symbol[:15]]
        logger.error(
            f"Symbol '{symbol_name}' not found. "
            f"First 15 available: {available}"
        )
        return None
