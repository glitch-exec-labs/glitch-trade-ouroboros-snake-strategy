"""
Adaptive position sizing for ml_collector bots.

Combines two effects:
1. Live balance compounding — base notional = balance × notional_pct
2. Rolling win-rate multiplier — scaled to [0.5, 1.5] based on last N closed
   trades for that bot, so hot streaks size up and cold streaks size down
   faster than plain equity compounding.

Formula:
    base_notional   = balance × notional_pct
    target_notional = base_notional × streak_mult
    wire_volume     = target_notional / (price × 0.01)    # USD notional / per-wire-unit value
    lots            = wire_volume / lot_size

Caches balance per account (60s TTL) to avoid hammering cTrader on every trade.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid
from typing import Optional

import asyncpg

from . import _ctrader_compat  # noqa: F401
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderReq, ProtoOATraderRes

from .order_placer import SymbolSpec

logger = logging.getLogger("ml_collector.sizer")

PT_TRADER_REQ = 2121
PT_TRADER_RES = 2122
PT_HEARTBEAT = 51


class BalanceCache:
    """Per-account balance cache with 60s TTL."""

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._entries: dict[int, tuple[float, float]] = {}  # account_id -> (balance, fetched_at)
        self._lock = asyncio.Lock()

    async def get(self, client, account_id: int) -> float:
        now = time.monotonic()
        entry = self._entries.get(account_id)
        if entry and (now - entry[1]) < self._ttl:
            return entry[0]

        async with self._lock:
            entry = self._entries.get(account_id)
            if entry and (time.monotonic() - entry[1]) < self._ttl:
                return entry[0]
            balance = await _fetch_balance(client, account_id)
            if balance is not None and balance > 0:
                self._entries[account_id] = (balance, time.monotonic())
                return balance
            if entry:
                logger.warning("balance fetch failed for %d, reusing stale %.2f", account_id, entry[0])
                return entry[0]
            logger.warning("balance fetch failed for %d, no cached value; using 50000", account_id)
            return 50000.0  # fall back to nominal $50K


async def _fetch_balance(client, account_id: int) -> Optional[float]:
    async def op(reader, writer):
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = account_id
        mid = str(uuid.uuid4())[:8]
        writer.write(client._build_frame(PT_TRADER_REQ, req.SerializeToString(), mid))  # noqa: SLF001
        await writer.drain()

        for _ in range(10):
            hdr = await asyncio.wait_for(reader.readexactly(4), timeout=10)
            length = struct.unpack(">I", hdr)[0]
            data = await asyncio.wait_for(reader.readexactly(length), timeout=10)
            m = ProtoMessage()
            m.ParseFromString(data)
            if m.payloadType == PT_HEARTBEAT:
                continue
            if m.payloadType == PT_TRADER_RES:
                res = ProtoOATraderRes()
                res.ParseFromString(m.payload)
                money_digits = getattr(res.trader, "moneyDigits", 2) or 2
                return res.trader.balance / (10 ** money_digits)
        return None

    try:
        return await client._session(op)  # noqa: SLF001
    except Exception as e:
        logger.exception("fetch_balance failed for %d: %s", account_id, e)
        return None


async def rolling_win_rate(pool: asyncpg.Pool, bot_name: str, window: int = 10) -> tuple[float, int]:
    """
    Returns (win_rate, sample_count).  Ignores UNKNOWN outcomes.
    Returns (0.5, 0) if too few closed trades — caller defaults to 1.0× multiplier.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT outcome FROM ml_trades
               WHERE bot_name = $1 AND outcome IN ('WIN', 'LOSS')
               ORDER BY closed_at DESC NULLS LAST
               LIMIT $2""",
            bot_name, window,
        )
    n = len(rows)
    if n == 0:
        return (0.5, 0)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    return (wins / n, n)


def streak_multiplier(win_rate: float, sample_count: int, min_samples: int = 3) -> float:
    """
    Maps win_rate ∈ [0, 1] → multiplier ∈ [0.5, 1.5].
    Returns 1.0 if not enough samples yet.
        0% wins  -> 0.5x (defensive halving)
       50% wins  -> 1.0x (neutral)
      100% wins  -> 1.5x (aggressive)
    """
    if sample_count < min_samples:
        return 1.0
    return max(0.5, min(1.5, 0.5 + win_rate))


def compute_adaptive_lots(
    balance: float,
    notional_pct: float,
    streak_mult: float,
    price: float,
    spec: SymbolSpec,
) -> tuple[float, int]:
    """
    Returns (lots, wire_volume), both clamped to the symbol's [min, max] with step.

    wire_volume is the value passed to cTrader directly; lots is the derived
    lot count for logging only.

    Notional model:
      1 wire unit = (1/100) × base_units_per_lot ... but cTrader treats
      `volume` as (base_units × 100) so wire_volume per 1 unit of price =
      wire_volume * (1/100) USD for USD-quoted symbols.
      target_wire = target_notional × 100 / price
      This is a good approximation for USD-quoted FX, metals, and indices
      where lot_size reflects actual base-unit count.
    """
    target_notional = max(0.0, balance) * max(0.0, notional_pct) * max(0.0, streak_mult)
    if target_notional <= 0 or price <= 0:
        wire = spec.min_volume
    else:
        wire = int(round(target_notional * 100.0 / price))

    step = max(spec.step_volume, 1)
    stepped = max(step, (wire // step) * step)
    wire = max(min(stepped, spec.max_volume), spec.min_volume)

    lots = wire / spec.lot_size if spec.lot_size > 0 else 0.0
    return (lots, wire)
