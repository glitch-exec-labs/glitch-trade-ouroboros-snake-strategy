"""
GlitchExecutor - cTrader Open API Client

Async TCP client for the cTrader Open API (live + demo accounts).
Uses asyncio streams directly — no Twisted dependency.
Matches the same interface as MT5Client and ExchangeClient.

Proto messages are built and parsed via the vendored Protobuf helper
(executor/protobuf.py, sourced from spotware/OpenApiPy). No manual
payload-type constants needed — any new cTrader message type works
automatically after a `pip install -U ctrader-open-api`.
"""
import asyncio
import ssl
import struct
import uuid
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("CTraderClient")

LIVE_HOST = "live.ctraderapi.com"
DEMO_HOST = "demo.ctraderapi.com"
PORT = 5035  # SSL

try:
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
    from .protobuf import Protobuf
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
            "CTraderClient ready — account %s (%s) via %s:%d",
            account_id, "LIVE" if live else "DEMO", self.host, PORT,
        )

    # ── Low-level TCP helpers ─────────────────────────────────────────────────

    async def _open_connection(self):
        ssl_ctx = ssl.create_default_context()
        return await asyncio.open_connection(self.host, PORT, ssl=ssl_ctx)

    def _build_frame(self, msg_obj, client_msg_id: str = None) -> bytes:
        """
        Wrap a proto message object in a ProtoMessage envelope and prefix
        with a 4-byte big-endian length.  The payloadType is derived from
        the message itself — no manual PT_ constants required.
        """
        wrapper = ProtoMessage()
        wrapper.payloadType = msg_obj.payloadType
        wrapper.payload = msg_obj.SerializeToString()
        if client_msg_id:
            wrapper.clientMsgId = client_msg_id
        encoded = wrapper.SerializeToString()
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
        Read frames until one matches expected_types (list of int payloadTypes).
        Silently discards heartbeats. Raises on error responses.
        """
        _hb  = Protobuf.get_type("HeartbeatEvent")
        _err = Protobuf.get_type("ErrorRes")
        while True:
            msg = await self._recv_frame(reader)

            if msg.payloadType == _hb:
                continue

            if msg.payloadType == _err:
                err = Protobuf.extract(msg)
                raise RuntimeError(
                    f"cTrader API error {err.errorCode}: {err.description}"
                )

            # Skip unrelated responses when waiting for a specific msgId
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
            req = Protobuf.get("ApplicationAuthReq",
                               clientId=self.client_id,
                               clientSecret=self.client_secret)
            mid = str(uuid.uuid4())[:8]
            writer.write(self._build_frame(req, mid))
            await writer.drain()
            await self._recv_until(reader, [Protobuf.get_type("ApplicationAuthRes")], mid)
            logger.debug("App auth OK")

            # 2. Account authentication
            req2 = Protobuf.get("AccountAuthReq",
                                ctidTraderAccountId=self.account_id,
                                accessToken=self.access_token)
            mid2 = str(uuid.uuid4())[:8]
            writer.write(self._build_frame(req2, mid2))
            await writer.drain()
            await self._recv_until(reader, [Protobuf.get_type("AccountAuthRes")], mid2)
            logger.debug("Account %d auth OK", self.account_id)

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
            logger.error("cTrader health check failed: %s", e)
            return False

    async def _health_async(self) -> bool:
        try:
            await self._session(lambda r, w: asyncio.sleep(0))
            return True
        except Exception as e:
            logger.error("cTrader health check error: %s", e)
            return False

    async def get_balance(self) -> Dict:
        """Get account balance."""
        try:
            async def fetch(reader, writer):
                req = Protobuf.get("TraderReq",
                                   ctidTraderAccountId=self.account_id)
                mid = str(uuid.uuid4())[:8]
                writer.write(self._build_frame(req, mid))
                await writer.drain()
                res_msg = await self._recv_until(
                    reader, [Protobuf.get_type("TraderRes")], mid
                )
                res = Protobuf.extract(res_msg)
                money_digits = getattr(res.trader, "moneyDigits", 2) or 2
                balance = res.trader.balance / (10 ** money_digits)
                return {
                    "total":    round(balance, 2),
                    "free":     round(balance, 2),
                    "equity":   round(balance, 2),
                    "currency": "USD",
                }

            return await self._session(fetch)

        except Exception as e:
            logger.error("get_balance failed: %s", e)
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

                req = Protobuf.get("NewOrderReq")
                req.ctidTraderAccountId = self.account_id
                req.symbolId  = symbol_id
                req.orderType = 1 if price is None else 2  # MARKET=1, LIMIT=2
                req.tradeSide = 1 if side.lower() == "buy" else 2  # BUY=1, SELL=2
                req.volume    = max(1, int(round(amount * 100)))   # centilots
                if price is not None:
                    req.limitPrice = price
                if sl is not None:
                    req.stopLoss = sl
                if tp is not None:
                    req.takeProfit = tp

                writer.write(self._build_frame(req))
                await writer.drain()

                res_msg = await self._recv_until(
                    reader, [Protobuf.get_type("ExecutionEvent")]
                )
                ev  = Protobuf.extract(res_msg)
                pos = ev.position if ev.HasField("position") else None
                return {
                    "success":     True,
                    "order_id":    str(ev.order.orderId),
                    "position_id": str(pos.positionId) if pos else None,
                    "symbol":      symbol,
                    "side":        side.upper(),
                    "amount":      amount,
                    "price":       pos.price if pos else (price or 0),
                    "status":      "filled",
                    "sl":          sl,
                    "tp":          tp,
                    "message":     f"{side.upper()} {amount} lot(s) {symbol}",
                }

            return await self._session(send)

        except Exception as e:
            logger.error("place_order failed: %s", e)
            return {"success": False, "error": str(e), "symbol": symbol}

    async def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        try:
            return await self._session(self._reconcile)
        except Exception as e:
            logger.error("get_open_positions failed: %s", e)
            return []

    async def close_position(self, symbol: str, position_id: str) -> Dict:
        """Close a position by position ID."""
        try:
            async def close(reader, writer):
                positions = await self._reconcile(reader, writer)
                volume = None
                for p in positions:
                    if p["ticket"] == str(position_id):
                        volume = int(round(p["amount"] * 100))
                        break

                if volume is None:
                    return {
                        "success": False,
                        "error":   f"Position {position_id} not found",
                        "ticket":  position_id,
                    }

                req = Protobuf.get("ClosePositionReq",
                                   ctidTraderAccountId=self.account_id,
                                   positionId=int(position_id),
                                   volume=volume)
                writer.write(self._build_frame(req))
                await writer.drain()
                await self._recv_until(reader, [Protobuf.get_type("ExecutionEvent")])
                return {"success": True, "message": f"Closed position #{position_id}",
                        "ticket": position_id}

            return await self._session(close)

        except Exception as e:
            logger.error("close_position failed: %s", e)
            return {"success": False, "error": str(e), "ticket": position_id}

    async def modify_position(self, position_id: str,
                              sl: float = None, tp: float = None) -> Dict:
        """Modify SL/TP on an open position."""
        try:
            async def modify(reader, writer):
                req = Protobuf.get("AmendPositionSLTPReq",
                                   ctidTraderAccountId=self.account_id,
                                   positionId=int(position_id))
                if sl is not None:
                    req.stopLoss = sl
                if tp is not None:
                    req.takeProfit = tp

                writer.write(self._build_frame(req))
                await writer.drain()
                await self._recv_until(reader, [Protobuf.get_type("ExecutionEvent")])
                return {"success": True, "message": f"Modified position #{position_id}",
                        "ticket": position_id, "sl": sl, "tp": tp}

            return await self._session(modify)

        except Exception as e:
            logger.error("modify_position failed: %s", e)
            return {"success": False, "error": str(e), "ticket": position_id}

    async def get_deals_by_position(
        self, position_id: str, from_ts_ms: int, to_ts_ms: int
    ) -> List[Dict]:
        """
        Fetch all deals for a position (ProtoOADealListByPositionIdReq).
        Returns a list of deal dicts; closing deals have is_close=True and
        include gross_profit and balance from closePositionDetail.
        """
        try:
            async def fetch(reader, writer):
                req = Protobuf.get("DealListByPositionIdReq",
                                   ctidTraderAccountId=self.account_id,
                                   positionId=int(position_id),
                                   fromTimestamp=from_ts_ms,
                                   toTimestamp=to_ts_ms)
                mid = str(uuid.uuid4())[:8]
                writer.write(self._build_frame(req, mid))
                await writer.drain()
                res_msg = await self._recv_until(
                    reader, [Protobuf.get_type("DealListByPositionIdRes")], mid
                )
                res = Protobuf.extract(res_msg)
                deals = []
                for d in res.deal:
                    deal = {
                        "deal_id":         d.dealId,
                        "execution_price": float(d.executionPrice) if d.executionPrice else 0.0,
                        "is_close":        d.HasField("closePositionDetail"),
                    }
                    if d.HasField("closePositionDetail"):
                        cpd     = d.closePositionDetail
                        divisor = 10 ** (cpd.moneyDigits if cpd.moneyDigits else 2)
                        deal["gross_profit"] = cpd.grossProfit / divisor
                        deal["balance"]      = cpd.balance / divisor
                    deals.append(deal)
                return deals

            return await self._session(fetch)
        except Exception as e:
            # INCORRECT_BOUNDARIES / server-side window rejections are recoverable —
            # monitor_loop falls back to bar-price estimation for exit_price + PnL.
            # Log at DEBUG so ops logs stay readable; real failures still surface
            # via the WARNING for "unexpected_deal_history_error" below.
            msg = str(e)
            if "INCORRECT_BOUNDARIES" in msg or "ErrorCode 60" in msg:
                logger.debug("get_deals_by_position(%s) window rejected: %s", position_id, msg)
            else:
                logger.warning("get_deals_by_position(%s) unexpected_deal_history_error: %s",
                               position_id, msg)
            return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _reconcile(self, reader, writer) -> List[Dict]:
        """Fetch open positions within an existing authenticated session."""
        req = Protobuf.get("ReconcileReq",
                           ctidTraderAccountId=self.account_id)
        mid = str(uuid.uuid4())[:8]
        writer.write(self._build_frame(req, mid))
        await writer.drain()

        res_msg = await self._recv_until(
            reader, [Protobuf.get_type("ReconcileRes")], mid
        )
        res = Protobuf.extract(res_msg)

        positions = []
        for pos in res.position:
            td = pos.tradeData
            positions.append({
                "ticket":      str(pos.positionId),
                "symbol":      str(td.symbolId),
                "side":        "BUY" if td.tradeSide == 1 else "SELL",  # BUY=1, SELL=2
                "amount":      td.volume / 100.0,  # centilots → lots
                "entry_price": pos.price if pos.HasField("price") else 0,
                "sl":          pos.stopLoss if pos.HasField("stopLoss") else None,
                "tp":          pos.takeProfit if pos.HasField("takeProfit") else None,
                # NOTE: ProtoOAPosition has no unrealizedPnl field; use
                # get_deals_by_position() after close for authoritative P&L.
                "profit":      0,
            })
        return positions

    async def _lookup_symbol(self, reader, writer, symbol_name: str) -> Optional[int]:
        """Look up the numeric symbolId for a symbol name within a session."""
        req = Protobuf.get("SymbolsListReq",
                           ctidTraderAccountId=self.account_id,
                           includeArchivedSymbols=False)
        mid = str(uuid.uuid4())[:8]
        writer.write(self._build_frame(req, mid))
        await writer.drain()

        res_msg = await self._recv_until(
            reader, [Protobuf.get_type("SymbolsListRes")], mid
        )
        res = Protobuf.extract(res_msg)

        for sym in res.symbol:
            if sym.symbolName.upper() == symbol_name.upper():
                return sym.symbolId

        available = [s.symbolName for s in res.symbol[:15]]
        logger.error("Symbol '%s' not found. First 15 available: %s",
                     symbol_name, available)
        return None
