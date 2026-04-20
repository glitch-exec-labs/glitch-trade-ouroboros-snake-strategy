"""
Correct cTrader order placement with per-symbol spec caching.

The production executor/ctrader_client.py::place_order() uses a buggy volume
formula: ``req.volume = max(1, int(round(amount * 100)))``. cTrader's wire
encoding actually requires ``volume = lots * lotSize / 100`` where lotSize
is fetched per-symbol via ProtoOASymbolByIdReq.

This module:
  1. Caches (account_id, symbol) → SymbolSpec on first use
  2. Converts (lots, spec) → wire volume correctly, clamped to step + min/max
  3. Sends ProtoOANewOrderReq and reads the response, handling both
     PT_EXECUTION_EVENT (success) and PT_ORDER_ERROR_EVENT=2132 (failure),
     which the production client does not handle
"""
from __future__ import annotations

import asyncio
import logging
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Import patch MUST be applied before executor.ctrader_client is imported anywhere.
from . import _ctrader_compat  # noqa: F401

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)

logger = logging.getLogger("ml_collector.order_placer")

# Payload types used here
PT_NEW_ORDER_REQ = 2106
PT_SYMBOL_BY_ID_REQ = 2116
PT_SYMBOL_BY_ID_RES = 2117
PT_EXECUTION_EVENT = 2126
PT_ORDER_ERROR_EVENT = 2132
PT_ERROR_RES = 2142
PT_HEARTBEAT = 51


@dataclass(frozen=True)
class SymbolSpec:
    symbol_id: int
    min_volume: int   # wire units
    max_volume: int
    step_volume: int
    lot_size: int     # wire units per standard lot
    digits: int


class SymbolSpecCache:
    """
    (account_id, symbol_name_upper) -> SymbolSpec, lazily populated.
    Thread-safe via asyncio.Lock per cache instance.
    """
    def __init__(self):
        self._entries: Dict[Tuple[int, str], SymbolSpec] = {}
        self._lock = asyncio.Lock()

    async def get(self, client, account_id: int, symbol: str) -> Optional[SymbolSpec]:
        key = (account_id, symbol.upper())
        if key in self._entries:
            return self._entries[key]

        async with self._lock:
            if key in self._entries:  # check again after await
                return self._entries[key]

            spec = await _fetch_spec(client, account_id, symbol)
            if spec is not None:
                self._entries[key] = spec
                logger.info(
                    "Cached %s@%d: symbol_id=%d min=%d step=%d lotSize=%d digits=%d",
                    symbol, account_id, spec.symbol_id, spec.min_volume,
                    spec.step_volume, spec.lot_size, spec.digits,
                )
            return spec


async def _fetch_spec(client, account_id: int, symbol: str) -> Optional[SymbolSpec]:
    async def op(reader, writer):
        sid = await client._lookup_symbol(reader, writer, symbol)  # noqa: SLF001
        if sid is None:
            return None

        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = account_id
        req.symbolId.append(sid)
        mid = str(uuid.uuid4())[:8]
        writer.write(client._build_frame(req, mid))  # noqa: SLF001
        await writer.drain()

        res_msg = await client._recv_until(reader, [PT_SYMBOL_BY_ID_RES], mid)  # noqa: SLF001
        res = ProtoOASymbolByIdRes()
        res.ParseFromString(res_msg.payload)
        if not res.symbol:
            return None
        s = res.symbol[0]
        return SymbolSpec(
            symbol_id=sid,
            min_volume=int(s.minVolume),
            max_volume=int(s.maxVolume),
            step_volume=int(s.stepVolume) or 1,
            lot_size=int(getattr(s, "lotSize", 0) or 0),
            digits=int(getattr(s, "digits", 5) or 5),
        )

    try:
        return await client._session(op)  # noqa: SLF001
    except Exception:
        logger.exception("symbol spec fetch failed for %s@%d", symbol, account_id)
        return None


def lots_to_wire(lots: float, spec: SymbolSpec) -> int:
    """
    Convert desired position size in standard lots to cTrader wire volume.

    wire = lots × lotSize / 100, rounded to the nearest step, clamped to
    [min_volume, max_volume]. If the requested size rounds to zero, we
    clamp up to min_volume rather than skipping the trade.
    """
    if spec.lot_size <= 0:
        # Fallback: old buggy formula. Shouldn't happen in practice.
        raw = int(round(lots * 100))
    else:
        raw = int(round(lots * spec.lot_size))

    step = max(spec.step_volume, 1)
    stepped = max(step, (raw // step) * step)
    return max(min(stepped, spec.max_volume), spec.min_volume)


async def place_market_order(
    client,
    spec: SymbolSpec,
    account_id: int,
    symbol: str,
    side: str,   # "buy" or "sell"
    lots: float,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    entry_price: Optional[float] = None,
    wire_volume: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Send a ProtoOANewOrderReq and return a place_order-compatible dict.

    Unlike executor/ctrader_client.py::place_order, this:
      - uses the real wire-volume encoding from the symbol spec
      - handles ProtoOAOrderErrorEvent (payloadType 2132) as a first-class failure
      - reports the executed fill price from the returned position
    """
    if wire_volume is None:
        wire_volume = lots_to_wire(lots, spec)

    async def op(reader, writer):
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = account_id
        req.symbolId = spec.symbol_id
        req.orderType = ProtoOAOrderType.Value("MARKET")
        req.tradeSide = ProtoOATradeSide.Value("BUY" if side.lower() == "buy" else "SELL")
        req.volume = wire_volume
        # MARKET orders require relativeStopLoss/relativeTakeProfit (in 1/100000 price units)
        # rather than absolute stopLoss/takeProfit (which are LIMIT/STOP only).
        if entry_price is not None and entry_price > 0:
            # relativeSL/TP are in 1/100000 of price unit but must be a multiple of
            # 10**(5 - digits) so the resulting SL/TP lands on a valid price step.
            prec = 10 ** max(0, 5 - spec.digits)
            if sl is not None:
                raw = int(round(abs(float(entry_price) - float(sl)) * 100000))
                req.relativeStopLoss = max(prec, (raw // prec) * prec)
            if tp is not None:
                raw = int(round(abs(float(tp) - float(entry_price)) * 100000))
                req.relativeTakeProfit = max(prec, (raw // prec) * prec)

        mid = str(uuid.uuid4())[:8]
        writer.write(client._build_frame(req, mid))  # noqa: SLF001
        await writer.drain()

        # Read response frames until we see either an execution event or an order error.
        # Ignore heartbeats and unrelated events.
        for _ in range(10):
            hdr = await asyncio.wait_for(reader.readexactly(4), timeout=15)
            length = struct.unpack(">I", hdr)[0]
            data = await asyncio.wait_for(reader.readexactly(length), timeout=15)
            m = ProtoMessage()
            m.ParseFromString(data)

            if m.payloadType == PT_HEARTBEAT:
                continue

            if m.payloadType == PT_ORDER_ERROR_EVENT:
                err = ProtoOAOrderErrorEvent()
                err.ParseFromString(m.payload)
                return {
                    "success": False,
                    "error": f"{err.errorCode}: {err.description}",
                    "symbol": symbol,
                }

            if m.payloadType == PT_ERROR_RES:
                err2 = ProtoOAErrorRes()
                err2.ParseFromString(m.payload)
                return {
                    "success": False,
                    "error": f"{err2.errorCode}: {err2.description}",
                    "symbol": symbol,
                }

            if m.payloadType == PT_EXECUTION_EVENT:
                ev = ProtoOAExecutionEvent()
                ev.ParseFromString(m.payload)
                order = ev.order if ev.HasField("order") else None
                pos = ev.position if ev.HasField("position") else None
                filled_price = pos.price if pos and pos.HasField("price") else 0.0
                return {
                    "success": True,
                    "order_id": str(order.orderId) if order else "",
                    "position_id": str(pos.positionId) if pos else None,
                    "symbol": symbol,
                    "side": side.upper(),
                    "amount": lots,
                    "wire_volume": wire_volume,
                    "price": filled_price,
                    "status": "filled",
                    "sl": sl,
                    "tp": tp,
                }

            logger.debug("ignoring payloadType=%d during order response", m.payloadType)

        return {"success": False, "error": "no response frames matched", "symbol": symbol}

    try:
        return await client._session(op)  # noqa: SLF001
    except Exception as e:
        logger.exception("place_market_order failed for %s@%d", symbol, account_id)
        return {"success": False, "error": f"{type(e).__name__}: {e}", "symbol": symbol}
