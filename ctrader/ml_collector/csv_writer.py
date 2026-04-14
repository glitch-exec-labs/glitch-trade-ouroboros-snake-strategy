"""
Crash-safe, daily-rotated CSV writer with atomic row updates.

- append_signal writes one row per evaluation, fsynced to disk
- update_outcome rewrites a single row (located by row_id) atomically
  via tempfile + os.replace, scanning today's, yesterday's, and
  day-before-yesterday's file (trades can straddle midnight)
- fcntl.flock guarantees no interleaving between the signal loop and
  the monitor loop, even on crash-restart
"""
from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .state import OpenTrade, Signal

logger = logging.getLogger("ml_collector.csv_writer")


# Canonical column order — matches the plan's schema exactly.
SCHEMA: List[str] = [
    # Identity
    "row_id", "timestamp", "strategy", "symbol", "bot", "account", "timeframe",
    # Signal
    "signal", "signal_type", "confidence", "reasoning",
    # Trade execution
    "executed", "entry_price", "sl_price", "tp_price", "volume_lots", "ticket",
    # Trade outcome (filled later by monitor loop)
    "exit_price", "exit_reason", "profit", "pnl", "outcome", "duration_minutes",
    "account_balance", "account_equity",
    # Bar snapshot at signal time
    "bar_open", "bar_high", "bar_low", "bar_close",
    # Momentum-hunter indicators
    "rsi", "ema_20", "price_above_ema", "volume_ratio", "rsi_crossover",
    # Mamba-reversion indicators
    "bb_upper", "bb_mid", "bb_lower", "bb_width", "price_position_in_bb",
    "adx", "atr", "regime", "trigger",
    # Safety net for anything new a model emits
    "indicators_json",
]


def _empty_row() -> Dict[str, str]:
    return {k: "" for k in SCHEMA}


def _flat_indicator(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (list, dict)):
        return json.dumps(val, separators=(",", ":"), default=str)
    return str(val)


class DailyCSVWriter:
    """One writer per strategy. Files named <strategy>_signals_YYYY-MM-DD.csv."""

    def __init__(self, strategy: str, base_dir: Path, account_id: int):
        self.strategy = strategy
        self.account_id = account_id
        self.dir = Path(base_dir) / strategy
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Tracks the bar_close of the last successfully-appended row (of any type).
        # Used to deduplicate HOLD rows that repeat the same bar — BUY/SELL rows
        # are always written regardless, and they reset this to the new value.
        self._last_bar_close: Optional[float] = None

    # ── paths ────────────────────────────────────────────────────────────

    def _path_for(self, dt: datetime) -> Path:
        return self.dir / f"{self.strategy}_signals_{dt.strftime('%Y-%m-%d')}.csv"

    def _ensure_file(self, path: Path) -> None:
        if not path.exists():
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=SCHEMA)
                writer.writeheader()
                f.flush()
                os.fsync(f.fileno())

    # ── row construction ────────────────────────────────────────────────

    def _build_row(
        self,
        signal: Signal,
        executed: bool,
        trade: Optional[OpenTrade],
    ) -> Dict[str, str]:
        row = _empty_row()
        row["row_id"] = str(uuid.uuid4())
        row["timestamp"] = signal.timestamp.isoformat()
        row["strategy"] = signal.strategy
        row["symbol"] = signal.symbol
        row["bot"] = signal.strategy
        row["account"] = str(self.account_id)
        row["timeframe"] = signal.timeframe
        row["signal"] = signal.vote
        row["signal_type"] = signal.model_name
        row["confidence"] = f"{signal.confidence:.4f}"
        row["reasoning"] = signal.reasoning
        row["executed"] = "true" if executed else "false"

        if trade is not None:
            row["entry_price"] = f"{trade.entry_price:.6f}"
            row["sl_price"] = f"{trade.sl_price:.6f}"
            row["tp_price"] = f"{trade.tp_price:.6f}"
            row["volume_lots"] = f"{trade.volume_lots:.4f}"
            row["ticket"] = trade.ticket

        row["bar_open"] = f"{signal.bar_open:.6f}"
        row["bar_high"] = f"{signal.bar_high:.6f}"
        row["bar_low"] = f"{signal.bar_low:.6f}"
        row["bar_close"] = f"{signal.bar_close:.6f}"

        # Flatten known indicator keys into typed columns; keep everything in indicators_json.
        ind = signal.indicators or {}
        for key in (
            "rsi", "ema_20", "price_above_ema", "volume_ratio", "rsi_crossover",
            "bb_upper", "bb_mid", "bb_lower", "bb_width", "price_position_in_bb",
            "adx", "atr", "regime", "trigger",
        ):
            if key in ind:
                row[key] = _flat_indicator(ind[key])
        row["indicators_json"] = json.dumps(ind, separators=(",", ":"), default=str)
        return row

    # ── append ──────────────────────────────────────────────────────────

    def append_signal(
        self,
        signal: Signal,
        executed: bool,
        trade: Optional[OpenTrade],
    ) -> Optional[str]:
        """
        Append a row for this signal.

        HOLD signals whose bar_close matches the previously-written row
        (regardless of that row's signal type) are skipped to prevent
        duplicate spam: Mamba reads closes[-2], so the same bar repeats
        for ~15 minutes at a 60s loop cadence. BUY/SELL signals are
        always written regardless of bar repetition.

        Returns the row_id of the written row, or None if skipped.
        """
        skip = (
            signal.vote == "HOLD"
            and executed is False
            and self._last_bar_close is not None
            and self._last_bar_close == signal.bar_close
        )
        if skip:
            return None

        row = self._build_row(signal, executed, trade)
        path = self._path_for(signal.timestamp)

        with self._lock:
            self._ensure_file(path)
            with open(path, "a", newline="") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    csv.DictWriter(f, fieldnames=SCHEMA).writerow(row)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        self._last_bar_close = signal.bar_close
        return row["row_id"]

    # ── update a row ────────────────────────────────────────────────────

    def update_outcome(self, row_id: str, **updates: Any) -> bool:
        """
        Locate a previously-written row by row_id in today's, yesterday's, or
        day-before-yesterday's file, and atomically rewrite it with new values.

        Returns True if the row was found and updated, False otherwise.
        """
        now = datetime.now(timezone.utc)
        candidates = [self._path_for(now - timedelta(days=d)) for d in range(3)]
        str_updates: Dict[str, str] = {}
        for k, v in updates.items():
            if k not in SCHEMA:
                logger.warning("update_outcome: ignoring unknown column %s", k)
                continue
            str_updates[k] = "" if v is None else str(v)

        if not str_updates:
            return False

        with self._lock:
            for path in candidates:
                if not path.exists():
                    continue
                tmp = path.with_suffix(".csv.tmp")
                found = False
                try:
                    with open(path, newline="") as fin, open(tmp, "w", newline="") as fout:
                        fcntl.flock(fin.fileno(), fcntl.LOCK_SH)
                        try:
                            reader = csv.DictReader(fin)
                            writer = csv.DictWriter(fout, fieldnames=SCHEMA)
                            writer.writeheader()
                            for r in reader:
                                if r.get("row_id") == row_id:
                                    r.update(str_updates)
                                    found = True
                                writer.writerow(r)
                            fout.flush()
                            os.fsync(fout.fileno())
                        finally:
                            fcntl.flock(fin.fileno(), fcntl.LOCK_UN)
                    if found:
                        os.replace(tmp, path)
                        return True
                    else:
                        tmp.unlink(missing_ok=True)
                except Exception:
                    logger.exception("update_outcome failed on %s", path)
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
            logger.warning(
                "update_outcome: row_id %s not found in last 3 days of %s",
                row_id, self.dir,
            )
            return False
