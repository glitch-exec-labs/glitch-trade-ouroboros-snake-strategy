"""
Smoke test that the two models can be imported and called with synthetic candles.

Does NOT connect to cTrader. Does NOT read the real .env.
Run: python -m ml_collector.tests.test_strategy_runner
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Vendored executor/ + ensemble/ sit one level above ml_collector/ in the ctrader repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Stub CTRADER_* env so CTraderPriceFeed instantiation doesn't explode on import.
os.environ.setdefault("CTRADER_CLIENT_ID", "stub")
os.environ.setdefault("CTRADER_CLIENT_SECRET", "stub")
os.environ.setdefault("CTRADER_ACCESS_TOKEN", "stub")
os.environ.setdefault("CTRADER_ACCOUNT_ID", "1")
os.environ.setdefault("CTRADER_LIVE", "false")


def _fake_candles(n: int = 120, base: float = 4500.0) -> np.ndarray:
    rng = np.random.default_rng(seed=42)
    ts = np.arange(n) * 900  # 15-minute bars in seconds
    closes = base + np.cumsum(rng.normal(0, 1.5, size=n))
    opens = closes + rng.normal(0, 0.3, size=n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.8, size=n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.8, size=n))
    vols = rng.uniform(800, 1500, size=n)
    return np.column_stack([ts, opens, highs, lows, closes, vols])


def test_models_return_signal() -> None:
    from ensemble.models.mamba_reversion import MambaReversionModel  # noqa: WPS433
    from ensemble.models.momentum_hunter import MomentumHunterModel  # noqa: WPS433

    candles = {"m15": _fake_candles(), "h1": _fake_candles(n=80, base=4510.0)}

    mh = MomentumHunterModel().analyze("XAUUSD", candles)
    assert "vote" in mh, mh
    assert mh["vote"] in ("BUY", "SELL", "HOLD"), mh
    assert "confidence" in mh

    mr = MambaReversionModel().analyze("EURUSD", candles)
    assert "vote" in mr, mr
    assert mr["vote"] in ("BUY", "SELL", "HOLD"), mr
    assert "confidence" in mr

    print(
        f"ok: momentum_hunter vote={mh['vote']} conf={mh.get('confidence'):.2f}, "
        f"mamba_reversion vote={mr['vote']} conf={mr.get('confidence'):.2f}"
    )


if __name__ == "__main__":
    test_models_return_signal()
    print("All strategy_runner tests passed")
