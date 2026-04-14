"""
GlitchExecutor Ensemble Models
Base model class for all trading analysis modules.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any
import numpy as np


class BaseModel(ABC):
    """Abstract base class all models must implement."""
    
    name: str = "base_model"
    version: str = "1.0"
    
    @abstractmethod
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """
        Analyze market data and return trading signal.
        
        Input:
            symbol: "BTCUSD", "EURUSD", etc.
            candles: {
                "m15": numpy array of [time, open, high, low, close, volume] — last 300 bars,
                "h1": numpy array — last 200 bars,
                "h4": numpy array — last 200 bars
            }
        
        Output:
            {
                "model": self.name,
                "vote": "BUY" | "SELL" | "HOLD",
                "confidence": float 0.0-1.0,
                "reasoning": str (1-2 sentences explaining why),
                "indicators": dict (key indicator values used in decision)
            }
        """
        raise NotImplementedError
    
    def _extract_ohlcv(self, candles: np.ndarray) -> tuple:
        """Extract OHLCV columns from candle array."""
        if candles is None or len(candles) == 0:
            return None, None, None, None, None
        
        time_col = candles[:, 0] if candles.shape[1] > 0 else None
        open_col = candles[:, 1] if candles.shape[1] > 1 else None
        high_col = candles[:, 2] if candles.shape[1] > 2 else None
        low_col = candles[:, 3] if candles.shape[1] > 3 else None
        close_col = candles[:, 4] if candles.shape[1] > 4 else None
        volume_col = candles[:, 5] if candles.shape[1] > 5 else None
        
        return open_col, high_col, low_col, close_col, volume_col
    
    def _safe_get_latest(self, arr: np.ndarray, n: int = 1) -> float:
        """Safely get the latest n values from an array."""
        if arr is None or len(arr) == 0:
            return None
        if n == 1:
            return float(arr[-1]) if not np.isnan(arr[-1]) else None
        return arr[-n:] if len(arr) >= n else arr
