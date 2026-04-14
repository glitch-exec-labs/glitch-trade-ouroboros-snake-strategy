"""
GlitchExecutor Model 5: Multi-Timeframe Alignment
Checks trend direction across M15, H1, and H4 timeframes for confirmation.
"""
import numpy as np
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import ema


class MultiTFAlignModel(BaseModel):
    """
    Multi-timeframe alignment strategy:
    - Checks if price is above/below EMA(20) on M15, H1, and H4
    - All 3 align = strong signal (0.9 confidence)
    - 2/3 align (incl. conflicting) = signal at varying confidence
    - Truly mixed (1/1/1) = HOLD
    """
    
    name = "multi_tf_align"
    version = "1.0"
    
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Check trend alignment across M15, H1, and H4."""
        
        # Check each timeframe
        m15_trend = self._get_trend_direction(candles.get("m15"))
        h1_trend = self._get_trend_direction(candles.get("h1"))
        h4_trend = self._get_trend_direction(candles.get("h4"))
        
        # Count bullish and bearish signals
        bullish_count = sum(1 for t in [m15_trend, h1_trend, h4_trend] if t == "bullish")
        bearish_count = sum(1 for t in [m15_trend, h1_trend, h4_trend] if t == "bearish")
        
        # Build indicators
        indicators = {
            "m15_trend": m15_trend,
            "h1_trend": h1_trend,
            "h4_trend": h4_trend,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count
        }
        
        # Determine alignment and generate signal
        if bullish_count == 3:
            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": 0.9,
                "reasoning": "All 3 timeframes (M15, H1, H4) aligned bullish — strong trend confirmation.",
                "indicators": indicators
            }
        
        elif bearish_count == 3:
            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": 0.9,
                "reasoning": "All 3 timeframes (M15, H1, H4) aligned bearish — strong trend confirmation.",
                "indicators": indicators
            }
        
        elif bullish_count == 2 and bearish_count == 0:
            # 2/3 bullish, 1 neutral — moderate confidence
            agreeing_tfs = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bullish"]

            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": 0.6,
                "reasoning": f"2/3 timeframes aligned bullish ({', '.join(agreeing_tfs)}) — moderate trend confirmation.",
                "indicators": indicators
            }

        elif bearish_count == 2 and bullish_count == 0:
            # 2/3 bearish, 1 neutral — moderate confidence
            agreeing_tfs = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bearish"]

            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": 0.6,
                "reasoning": f"2/3 timeframes aligned bearish ({', '.join(agreeing_tfs)}) — moderate trend confirmation.",
                "indicators": indicators
            }

        elif bullish_count == 2 and bearish_count == 1:
            # 2 bullish + 1 bearish — low confidence signal (was HOLD before)
            agreeing_tfs = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bullish"]
            opposing_tf = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bearish"][0]

            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": 0.45,
                "reasoning": f"2/3 timeframes bullish ({', '.join(agreeing_tfs)}) but {opposing_tf} bearish — weak bullish lean.",
                "indicators": indicators
            }

        elif bearish_count == 2 and bullish_count == 1:
            # 2 bearish + 1 bullish — low confidence signal (was HOLD before)
            agreeing_tfs = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bearish"]
            opposing_tf = [tf for tf, trend in [("M15", m15_trend), ("H1", h1_trend), ("H4", h4_trend)] if trend == "bullish"][0]

            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": 0.45,
                "reasoning": f"2/3 timeframes bearish ({', '.join(agreeing_tfs)}) but {opposing_tf} bullish — weak bearish lean.",
                "indicators": indicators
            }

        # Truly mixed signals (1 bullish, 1 bearish, 1 neutral)
        return {
            "model": self.name,
            "vote": "HOLD",
            "confidence": 0.5,
            "reasoning": f"Mixed timeframe signals (bullish: {bullish_count}, bearish: {bearish_count}) — no clear trend alignment.",
            "indicators": indicators
        }
    
    def _get_trend_direction(self, candles: np.ndarray) -> str:
        """Determine trend direction on a timeframe using EMA(20)."""
        if candles is None or len(candles) < 30:
            return "neutral"
        
        # Extract close prices
        closes = candles[:, 4] if candles.shape[1] > 4 else None
        if closes is None or len(closes) < 30:
            return "neutral"
        
        # Calculate EMA(20)
        ema_20 = ema(closes, 20)
        
        if len(ema_20) < 5:
            return "neutral"
        
        # Get current price and EMA
        current_price = closes[-1]
        current_ema = ema_20[-1]
        
        # Determine trend with threshold to avoid noise
        threshold = current_ema * 0.0003  # 0.03% threshold
        
        if current_price > current_ema + threshold:
            return "bullish"
        elif current_price < current_ema - threshold:
            return "bearish"
        return "neutral"
