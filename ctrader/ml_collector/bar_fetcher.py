"""
Batch bar fetcher with timeframe monkey-patch.

Extends the production CTraderPriceFeed to support M1, M5, M30 timeframes
(the production code only has M15, H1, H4) and provides bar deduplication
(skip model evaluation if the latest bar hasn't changed since last cycle).

We monkey-patch _TF_MAP at import time — this is safe because the production
price feed reads the dict at call time, not at import time.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

# Make the vendored executor/ ensemble/ packages importable (one level up from ml_collector/)
from pathlib import Path as _Path
_CTRADER_ROOT = str(_Path(__file__).resolve().parent.parent)
if _CTRADER_ROOT not in sys.path:
    sys.path.insert(0, _CTRADER_ROOT)

# Apply the cTrader compat patch BEFORE importing anything from executor/
from . import _ctrader_compat  # noqa: F401

# Monkey-patch the timeframe map to add M1, M5, M30
import ensemble.ctrader_price_feed as _pf  # noqa: E402

_pf._TF_MAP.update({
    "m1": 1, "1m": 1,
    "m5": 5, "5m": 5,
    "m30": 8, "30m": 8,
})

# Also extend fetch counts for the new timeframes
_pf._FETCH_COUNTS.update({
    "m1": 100,
    "m5": 200,
    "m30": 200,
})

from ensemble.ctrader_price_feed import CTraderPriceFeed  # noqa: E402

logger = logging.getLogger("ml_collector.bar_fetcher")


class BarFetcher:
    """
    Wraps CTraderPriceFeed to fetch bars for multiple symbols at a given
    timeframe and track which bars are new vs already-evaluated.
    """

    def __init__(self, feed: CTraderPriceFeed):
        self._feed = feed
        # Tracks the latest bar_time we've seen per (bot_name, symbol)
        self._last_bar_time: Dict[Tuple[str, str], float] = {}

    def fetch(self, symbol: str, timeframe: str, count: int) -> Optional[np.ndarray]:
        """
        Fetch bars for a single symbol at one timeframe.
        Returns numpy array of shape (N, 6): [time, open, high, low, close, volume]
        or None on failure.
        """
        try:
            bars = self._feed.get_candles(symbol, timeframe, count)
            if bars is not None and len(bars) >= 50:
                return bars
            if bars is not None:
                logger.debug(
                    "%s %s: only %d bars (need >=50), skipping",
                    symbol, timeframe, len(bars),
                )
            return None
        except Exception:
            logger.exception("Bar fetch failed for %s %s", symbol, timeframe)
            return None

    def is_new_bar(self, bot_name: str, symbol: str, bars: np.ndarray) -> bool:
        """
        Returns True if the latest bar in the array has a different timestamp
        than what we last saw for this (bot_name, symbol) combination.
        Always returns True on first call.
        """
        key = (bot_name, symbol)
        latest_time = float(bars[-1, 0])
        if self._last_bar_time.get(key) == latest_time:
            return False
        self._last_bar_time[key] = latest_time
        return True

    def bar_time_utc(self, bars: np.ndarray) -> datetime:
        """Extract the latest bar's timestamp as a UTC datetime."""
        return datetime.utcfromtimestamp(float(bars[-1, 0])).replace(tzinfo=timezone.utc)
