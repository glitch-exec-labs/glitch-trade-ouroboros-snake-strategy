"""
Unit test for DailyCSVWriter — writes a row, updates the outcome, reads it back.

Run: python -m ml_collector.tests.test_csv_writer
"""
from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure parent package is importable when running as __main__
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ml_collector.csv_writer import SCHEMA, DailyCSVWriter  # noqa: E402
from ml_collector.state import OpenTrade, Signal  # noqa: E402


def test_append_and_update() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ml_csv_test_"))
    try:
        writer = DailyCSVWriter(strategy="king_cobra", base_dir=tmp, account_id=999)

        sig = Signal(
            timestamp=datetime.now(timezone.utc),
            strategy="king_cobra",
            model_name="momentum_hunter",
            symbol="XAUUSD",
            timeframe="m15",
            vote="BUY",
            confidence=0.82,
            reasoning="RSI crossover above 52 with EMA confirmation",
            indicators={
                "rsi": 56.2,
                "ema_20": 4537.12,
                "price_above_ema": True,
                "volume_ratio": 1.4,
                "rsi_crossover": True,
                "atr": 6.2,
            },
            bar_open=4540.0,
            bar_high=4545.5,
            bar_low=4538.2,
            bar_close=4543.1,
        )

        trade = OpenTrade(
            row_id="__pending__",
            strategy="king_cobra",
            symbol="XAUUSD",
            account_id=999,
            side="BUY",
            entry_price=4543.1,
            sl_price=4533.8,
            tp_price=4555.5,
            volume_lots=0.01,
            ticket="pos-1234",
            opened_at=datetime.now(timezone.utc),
            signal_confidence=0.82,
            signal_indicators=sig.indicators,
        )

        row_id = writer.append_signal(sig, executed=True, trade=trade)
        assert row_id and row_id != "__pending__", f"unexpected row_id={row_id!r}"

        # Append a second HOLD row with same bar_close — should be skipped
        sig2 = Signal(
            timestamp=datetime.now(timezone.utc),
            strategy="king_cobra",
            model_name="momentum_hunter",
            symbol="XAUUSD",
            timeframe="m15",
            vote="HOLD",
            confidence=0.0,
            reasoning="Nothing",
            indicators={},
            bar_close=4543.1,  # same bar
        )
        writer.append_signal(sig2, executed=False, trade=None)  # should be skipped

        # Different bar_close — should be written
        sig3 = Signal(
            timestamp=datetime.now(timezone.utc),
            strategy="king_cobra",
            model_name="momentum_hunter",
            symbol="XAUUSD",
            timeframe="m15",
            vote="HOLD",
            confidence=0.0,
            reasoning="Nothing",
            indicators={},
            bar_close=4544.0,  # new bar
        )
        writer.append_signal(sig3, executed=False, trade=None)

        # Update the outcome of the first row
        ok = writer.update_outcome(
            row_id,
            exit_price=4555.5,
            exit_reason="tp_hit",
            profit=12.4,
            pnl=12.4,
            outcome="WIN",
            duration_minutes=42.3,
            account_balance=10012.4,
            account_equity=10012.4,
        )
        assert ok, "update_outcome should have found the row"

        # Verify
        csv_files = list((tmp / "king_cobra").glob("*.csv"))
        assert len(csv_files) == 1, f"expected 1 csv file, got {csv_files}"

        with open(csv_files[0]) as f:
            rows = list(csv.DictReader(f))

        # Should have exactly 2 data rows: the BUY (now updated) + the differing HOLD.
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}: {rows}"

        assert set(rows[0].keys()) == set(SCHEMA), "schema mismatch"
        target = next(r for r in rows if r["row_id"] == row_id)
        assert target["outcome"] == "WIN", target
        assert target["exit_reason"] == "tp_hit", target
        assert target["exit_price"] == "4555.5", target
        assert target["executed"] == "true", target
        assert target["signal"] == "BUY", target

        hold_row = next(r for r in rows if r["signal"] == "HOLD")
        assert hold_row["bar_close"] == "4544.000000", hold_row

        print("ok: test_append_and_update")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_append_and_update()
    print("All csv_writer tests passed")
