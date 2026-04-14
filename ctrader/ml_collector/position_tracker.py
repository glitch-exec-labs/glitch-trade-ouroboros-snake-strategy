"""
Tracks open positions across restarts and detects closures.

State is persisted to state/open_trades.json after every mutation via
atomic tempfile + os.replace, so a SIGKILL cannot corrupt the mapping.

Closures are approximate: without ProtoOAExecutionEvent push messages,
we infer SL vs TP vs manual from the current m15 close vs entry/sl/tp
on the first poll after the ticket disappears from the reconcile list.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .state import ClosureEvent, OpenTrade

logger = logging.getLogger("ml_collector.position_tracker")


class PositionTracker:
    def __init__(self, state_path: Path):
        self._state_path = Path(state_path)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._trades: Dict[str, OpenTrade] = {}  # row_id -> OpenTrade
        self._load()

    # ── persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            for row_id, d in data.items():
                try:
                    self._trades[row_id] = OpenTrade.from_json(d)
                except Exception:
                    logger.exception("Failed to load tracked trade %s — discarding", row_id)
            logger.info("Resumed %d tracked trades from %s", len(self._trades), self._state_path)
        except Exception:
            logger.exception("Failed to load open_trades.json — starting empty")

    def _persist(self) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        data = {row_id: t.to_json() for row_id, t in self._trades.items()}
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except Exception:
            logger.exception("Failed to persist open_trades.json")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── mutation ────────────────────────────────────────────────────────

    def register(self, trade: OpenTrade) -> None:
        with self._lock:
            self._trades[trade.row_id] = trade
            self._persist()
        logger.info(
            "Registered %s %s %s ticket=%s row_id=%s",
            trade.strategy, trade.side, trade.symbol, trade.ticket, trade.row_id,
        )

    def has_open(self, strategy: str, symbol: str) -> bool:
        with self._lock:
            return any(
                t.strategy == strategy and t.symbol == symbol
                for t in self._trades.values()
            )

    def count(self) -> int:
        with self._lock:
            return len(self._trades)

    # ── closure detection ──────────────────────────────────────────────

    async def poll_once(
        self,
        clients: Dict[int, "object"],
        current_prices: Dict[str, float],
    ) -> List[ClosureEvent]:
        """
        Poll each per-account cTrader client for open positions and detect
        any tracked trades that have vanished (meaning they closed).

        Args:
            clients: {account_id -> CTraderClient}
            current_prices: {symbol -> latest_close} used to classify closures
        """
        # Build per-account live-ticket sets.
        live_by_account: Dict[int, set[str]] = {}
        balances: Dict[int, tuple[float, float]] = {}

        for account_id, client in clients.items():
            try:
                positions = await client.get_open_positions()
                live_by_account[account_id] = {
                    str(p.get("ticket")) for p in positions if p.get("ticket")
                }
            except Exception:
                logger.exception("get_open_positions failed on account %s", account_id)
                # Don't classify this account's trades as closed on a transient
                # broker error — skip it this cycle.
                live_by_account[account_id] = None  # type: ignore[assignment]

            try:
                bal = await client.get_balance()
                balances[account_id] = (
                    float(bal.get("total", 0) or 0),
                    float(bal.get("equity", bal.get("total", 0)) or 0),
                )
            except Exception:
                balances[account_id] = (0.0, 0.0)

        with self._lock:
            closed_row_ids = []
            for rid, t in self._trades.items():
                live_set = live_by_account.get(t.account_id)
                if live_set is None:
                    continue  # skip on transient error
                if t.ticket not in live_set:
                    closed_row_ids.append(rid)

        if not closed_row_ids:
            return []

        events: List[ClosureEvent] = []
        now = datetime.now(timezone.utc)

        with self._lock:
            for row_id in closed_row_ids:
                trade = self._trades.pop(row_id, None)
                if trade is None:
                    continue
                current = current_prices.get(trade.symbol)
                balance, equity = balances.get(trade.account_id, (0.0, 0.0))
                ev = self._build_closure(trade, current, balance, equity, now)
                events.append(ev)
            self._persist()

        for ev in events:
            logger.info(
                "Closure detected: row_id=%s strategy=%s account=%s outcome=%s reason=%s pnl=%.2f",
                ev.row_id, ev.strategy, "?", ev.outcome, ev.exit_reason, ev.pnl,
            )
        return events

    def _build_closure(
        self,
        trade: OpenTrade,
        current: Optional[float],
        balance: float,
        equity: float,
        now: datetime,
    ) -> ClosureEvent:
        duration = max(0.0, (now - trade.opened_at).total_seconds() / 60.0)

        if current is None or current <= 0:
            return ClosureEvent(
                row_id=trade.row_id,
                strategy=trade.strategy,
                exit_price=0.0,
                exit_reason="manual_or_unknown",
                profit=0.0,
                pnl=0.0,
                outcome="UNKNOWN",
                duration_minutes=duration,
                account_balance=balance,
                account_equity=equity,
            )

        exit_price = current
        exit_reason, outcome = _classify(trade, current)
        # Rough PnL: (exit - entry) * direction * volume_lots * 100_000 for FX,
        # or * 100 for XAU. Use a generic contract multiplier; true pnl lives in
        # cTrader's books, this is only an approximation for the CSV.
        direction = 1 if trade.side.upper() == "BUY" else -1
        price_delta = (exit_price - trade.entry_price) * direction
        pnl = price_delta * trade.volume_lots * 100.0  # coarse approximation
        profit = pnl

        return ClosureEvent(
            row_id=trade.row_id,
            strategy=trade.strategy,
            exit_price=exit_price,
            exit_reason=exit_reason,
            profit=profit,
            pnl=pnl,
            outcome=outcome,
            duration_minutes=duration,
            account_balance=balance,
            account_equity=equity,
        )


def _classify(trade: OpenTrade, current: float) -> tuple[str, str]:
    """Infer exit reason + outcome from current price vs trade SL/TP."""
    side = trade.side.upper()
    sl = trade.sl_price
    tp = trade.tp_price

    if side == "BUY":
        if tp and current >= tp:
            return "tp_hit", "WIN"
        if sl and current <= sl:
            return "sl_hit", "LOSS"
        # No clean hit — use current relative to entry for a rough guess
        if current > trade.entry_price:
            return "manual_or_unknown", "WIN"
        if current < trade.entry_price:
            return "manual_or_unknown", "LOSS"
        return "manual_or_unknown", "UNKNOWN"

    # SELL
    if tp and current <= tp:
        return "tp_hit", "WIN"
    if sl and current >= sl:
        return "sl_hit", "LOSS"
    if current < trade.entry_price:
        return "manual_or_unknown", "WIN"
    if current > trade.entry_price:
        return "manual_or_unknown", "LOSS"
    return "manual_or_unknown", "UNKNOWN"
