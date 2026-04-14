"""
Async PostgreSQL writer for the ML collector v2.

Uses asyncpg with connection pooling. All writes are idempotent via
ON CONFLICT — safe to retry on crash/restart.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import numpy as np

logger = logging.getLogger("ml_collector.db")


async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, command_timeout=30)
    logger.info("Database pool created: %s (min=%d, max=%d)", dsn.split("@")[-1], min_size, max_size)
    return pool


class DatabaseWriter:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ── Bars ────────────────────────────────────────────────────────────

    async def insert_bars(self, symbol: str, timeframe: str, bars: np.ndarray) -> int:
        """
        Upsert OHLCV bars. Returns count of rows attempted (dupes are silently skipped).
        bars: numpy array shape (N, 6) — [time_epoch, open, high, low, close, volume]
        """
        if bars is None or len(bars) == 0:
            return 0

        rows = [
            (
                symbol,
                timeframe,
                datetime.utcfromtimestamp(float(bar[0])).replace(tzinfo=timezone.utc),
                float(bar[1]),
                float(bar[2]),
                float(bar[3]),
                float(bar[4]),
                float(bar[5]) if len(bar) > 5 else 0.0,
            )
            for bar in bars
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO ml_bars (symbol, timeframe, bar_time, open, high, low, close, volume)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (symbol, timeframe, bar_time) DO NOTHING""",
                rows,
            )
        return len(rows)

    # ── Signals ─────────────────────────────────────────────────────────

    async def insert_signal(
        self,
        bot_name: str,
        model_name: str,
        symbol: str,
        timeframe: str,
        account_id: int,
        vote: str,
        confidence: float,
        reasoning: str,
        bar_time: Optional[datetime],
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        indicators: Dict[str, Any],
        executed: bool = False,
    ) -> Optional[str]:
        """
        Insert a signal row. Returns signal_id (UUID string) or None if deduped.
        Dedup: (bot_name, symbol, bar_time) is unique — same bar = skip.
        """
        signal_id = str(uuid.uuid4())
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO ml_signals
                       (signal_id, bot_name, model_name, symbol, timeframe, account_id,
                        vote, confidence, reasoning, bar_time,
                        bar_open, bar_high, bar_low, bar_close,
                        indicators, executed)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                       ON CONFLICT (bot_name, symbol, bar_time) WHERE bar_time IS NOT NULL DO NOTHING""",
                    uuid.UUID(signal_id),
                    bot_name, model_name, symbol, timeframe, account_id,
                    vote, confidence, reasoning, bar_time,
                    bar_open, bar_high, bar_low, bar_close,
                    json.dumps(indicators, default=str),
                    executed,
                )
            return signal_id
        except asyncpg.UniqueViolationError:
            return None
        except Exception:
            logger.exception("insert_signal failed for %s/%s", bot_name, symbol)
            return None

    async def mark_signal_executed(self, signal_id: str, trade_db_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE ml_signals SET executed=TRUE, trade_id=$1 WHERE signal_id=$2",
                trade_db_id, uuid.UUID(signal_id),
            )

    # ── Trades ──────────────────────────────────────────────────────────

    async def insert_trade(
        self,
        signal_id: str,
        bot_name: str,
        model_name: str,
        symbol: str,
        timeframe: str,
        account_id: int,
        side: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        volume_lots: float,
        wire_volume: int,
        ticket: str,
        signal_confidence: float,
        signal_indicators: Dict[str, Any],
    ) -> Optional[int]:
        """Insert an open trade. Returns the DB row id."""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """INSERT INTO ml_trades
                       (signal_id, bot_name, model_name, symbol, timeframe, account_id,
                        side, entry_price, sl_price, tp_price, volume_lots, wire_volume,
                        ticket, signal_confidence, signal_indicators)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                       RETURNING id""",
                    uuid.UUID(signal_id),
                    bot_name, model_name, symbol, timeframe, account_id,
                    side, entry_price, sl_price, tp_price, volume_lots, wire_volume,
                    ticket, signal_confidence,
                    json.dumps(signal_indicators, default=str),
                )
                return row["id"]
        except Exception:
            logger.exception("insert_trade failed for %s/%s", bot_name, symbol)
            return None

    async def close_trade(
        self,
        trade_db_id: int,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        outcome: str,
        duration_minutes: float,
        account_balance: float,
        account_equity: float,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE ml_trades SET
                   exit_price=$1, exit_reason=$2, closed_at=NOW(),
                   pnl=$3, outcome=$4, duration_minutes=$5,
                   account_balance=$6, account_equity=$7
                   WHERE id=$8""",
                exit_price, exit_reason, pnl, outcome, duration_minutes,
                account_balance, account_equity, trade_db_id,
            )

    async def get_open_trades(self) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ml_trades WHERE closed_at IS NULL ORDER BY opened_at",
            )
            return [dict(r) for r in rows]

    async def has_open_trade(self, bot_name: str, symbol: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM ml_trades WHERE bot_name=$1 AND symbol=$2 AND closed_at IS NULL LIMIT 1",
                bot_name, symbol,
            )
            return row is not None

    # ── State ───────────────────────────────────────────────────────────

    async def save_state(self, key: str, value: Dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ml_collector_state (key, value, updated_at)
                   VALUES ($1, $2, NOW())
                   ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
                key, json.dumps(value, default=str),
            )

    async def load_state(self, key: str) -> Optional[Dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM ml_collector_state WHERE key=$1", key,
            )
            if row is None:
                return None
            val = row["value"]
            return json.loads(val) if isinstance(val, str) else val
