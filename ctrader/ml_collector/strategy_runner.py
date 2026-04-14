"""
Strategy runner v2 — loads ALL models, routes per-bot evaluation to the
correct model and timeframe.

Each bot has an assigned model (from the strategy-matrix.md). The model
always receives bars under the "m15" key because all existing models
read candles["m15"] — the model doesn't validate the actual bar interval.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Dict, Optional

from .config import BotConfig

logger = logging.getLogger("ml_collector.strategy_runner")

from pathlib import Path as _Path
_CTRADER_ROOT = str(_Path(__file__).resolve().parent.parent)
if _CTRADER_ROOT not in sys.path:
    sys.path.insert(0, _CTRADER_ROOT)


# Model registry — maps model names to their lazy-loaded instances.
_MODEL_CLASSES = {
    "momentum_hunter": ("ensemble.models.momentum_hunter", "MomentumHunterModel"),
    "mamba_reversion": ("ensemble.models.mamba_reversion", "MambaReversionModel"),
    "mean_reverter": ("ensemble.models.mean_reverter", "MeanReverterModel"),
    "trend_follower": ("ensemble.models.trend_follower", "TrendFollowerModel"),
    "volume_profiler": ("ensemble.models.volume_profiler", "VolumeProfilerModel"),
    "session_analyst": ("ensemble.models.session_analyst", "SessionAnalystModel"),
    "multi_tf_align": ("ensemble.models.multi_tf_align", "MultiTFAlignModel"),
}


class StrategyRunner:
    """Loads models on demand, caches them, evaluates per-bot with correct timeframe."""

    def __init__(self):
        self._models: Dict[str, Any] = {}

    def _get_model(self, model_name: str):
        if model_name in self._models:
            return self._models[model_name]

        if model_name not in _MODEL_CLASSES:
            raise ValueError(
                f"Unknown model {model_name!r}. "
                f"Available: {list(_MODEL_CLASSES.keys())}"
            )

        module_path, class_name = _MODEL_CLASSES[model_name]
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        instance = cls()
        self._models[model_name] = instance
        logger.info("Loaded model %s (%s.%s)", model_name, module_path, class_name)
        return instance

    def evaluate(self, bot: BotConfig, symbol: str, bars) -> Optional[Dict[str, Any]]:
        """
        Run the bot's assigned model on the provided bars.

        Args:
            bot: BotConfig with model name and timeframe
            symbol: e.g. "XAUUSD"
            bars: numpy array shape (N, 6) [time, open, high, low, close, volume]

        Returns:
            Model result dict {vote, confidence, reasoning, indicators} or None on failure.
        """
        model = self._get_model(bot.model)

        # All existing models read candles["m15"]. We pass whatever-timeframe
        # bars under that key so the model works unchanged.
        candles = {"m15": bars}

        try:
            result = model.analyze(symbol, candles)
        except Exception:
            logger.exception("%s.analyze() crashed on %s (bot=%s, tf=%s)",
                             bot.model, symbol, bot.name, bot.timeframe)
            return None

        return result
