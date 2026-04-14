"""
Live smoke test: connects to cTrader DEMO, fetches candles, runs both models,
prints the resulting signals. No trade is placed, no CSV is written.

Usage:
  python -m ml_collector.tests.test_smoke              # print only
  python -m ml_collector.tests.test_smoke --write      # also write 1 row each to /tmp/ml_smoke
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ml_collector.config import get_config, configure_logging  # noqa: E402
from ml_collector.csv_writer import DailyCSVWriter  # noqa: E402
from ml_collector.strategy_runner import StrategyRunner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Also write rows to --dir")
    parser.add_argument("--dir", default="/tmp/ml_smoke", help="Dir for --write rows")
    args = parser.parse_args()

    cfg = get_config()
    configure_logging(cfg)

    print(f"Price feed account: {cfg.price_feed_account_id} (demo)")
    print(f"Strategies: {[(s.name, s.account_id) for s in cfg.strategies]}")

    runner = StrategyRunner()

    if args.write:
        out_dir = Path(args.dir)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

    any_failed = False
    for strat in cfg.strategies:
        print(f"\n── Evaluating {strat.name} ({strat.symbol}, {strat.model}, account={strat.account_id})")
        sig = runner.evaluate(strat)
        if sig is None:
            print(f"  ! evaluation returned None (check broker connection / symbol)")
            any_failed = True
            continue
        print(f"  vote     : {sig.vote}")
        print(f"  confidence: {sig.confidence:.2f}")
        print(f"  reasoning: {sig.reasoning}")
        print(f"  bar_close: {sig.bar_close}")
        print(f"  indicators: {list(sig.indicators.keys())[:8]}{'...' if len(sig.indicators) > 8 else ''}")

        if args.write:
            writer = DailyCSVWriter(
                strategy=strat.name,
                base_dir=Path(args.dir),
                account_id=strat.account_id,
            )
            row_id = writer.append_signal(sig, executed=False, trade=None)
            print(f"  wrote row_id={row_id}")

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
