"""
GlitchExecutor Model 1: Trend Follower
Uses SMA/EMA crossover + ADX trend confirmation + ATR volatility filter.
"""
import numpy as np
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import sma, ema, adx, atr


class TrendFollowerModel(BaseModel):
    """
    Trend following strategy using:
    - SMA(9) / EMA(21) crossover detection within last 5 bars
    - ADX > 15 for trend confirmation
    - ATR vs median(ATR, 100) as confidence modifier (not a hard gate)
    """
    
    name = "trend_follower"
    version = "1.0"
    
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Run trend following analysis on H1 candles."""
        # Use H1 candles as specified
        h1_candles = candles.get("h1")
        
        if h1_candles is None or len(h1_candles) < 50:
            return {
                "model": self.name,
                "vote": "HOLD",
                "confidence": 0.0,
                "reasoning": "Insufficient H1 candle data for analysis.",
                "indicators": {}
            }
        
        # Extract OHLCV
        _, highs, lows, closes, _ = self._extract_ohlcv(h1_candles)
        
        if closes is None or len(closes) < 50:
            return {
                "model": self.name,
                "vote": "HOLD",
                "confidence": 0.0,
                "reasoning": "Invalid close price data.",
                "indicators": {}
            }
        
        # Calculate indicators
        sma_9 = sma(closes, 9)
        ema_21 = ema(closes, 21)
        adx_vals = adx(highs, lows, closes, 14)
        atr_vals = atr(highs, lows, closes, 14)
        
        # Get current values
        current_close = closes[-1]
        current_adx = adx_vals[-1] if not np.isnan(adx_vals[-1]) else 0
        current_atr = atr_vals[-1] if not np.isnan(atr_vals[-1]) else 0
        
        # Calculate median ATR over last 100 bars
        atr_100 = atr_vals[-100:] if len(atr_vals) >= 100 else atr_vals
        atr_median = np.median(atr_100[~np.isnan(atr_100)]) if len(atr_100) > 0 else 0
        
        # Detect crossover within last 5 bars
        crossover = self._detect_crossover_last_n(sma_9, ema_21, 5)
        
        # Build indicators dict
        indicators = {
            "sma_9": round(float(sma_9[-1]), 4) if len(sma_9) > 0 else None,
            "ema_21": round(float(ema_21[-1]), 4) if len(ema_21) > 0 else None,
            "adx": round(float(current_adx), 2),
            "atr": round(float(current_atr), 4),
            "atr_median_100": round(float(atr_median), 4),
            "crossover": crossover
        }
        
        # Check conditions — ADX 15 threshold (developing trends)
        trend_exists = current_adx > 15
        low_volatility = current_atr < atr_median

        # Determine signal — crossover + trend required, ATR is confidence modifier only
        if crossover == "bullish" and trend_exists:
            # Calculate confidence based on ADX strength
            if current_adx >= 25:
                confidence = 0.9
            elif current_adx >= 20:
                confidence = 0.75
            else:
                confidence = 0.6  # ADX 15-20: developing trend

            # ATR as confidence modifier (not a hard gate)
            if low_volatility:
                confidence = max(0.45, confidence - 0.15)
                atr_note = " Low ATR — reduced confidence."
            else:
                atr_note = ""

            reasoning = f"SMA(9) crossed above EMA(21) with ADX at {current_adx:.1f} (trend confirmed).{atr_note}"

            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": round(confidence, 2),
                "reasoning": reasoning,
                "indicators": indicators
            }

        elif crossover == "bearish" and trend_exists:
            # Calculate confidence based on ADX strength
            if current_adx >= 25:
                confidence = 0.9
            elif current_adx >= 20:
                confidence = 0.75
            else:
                confidence = 0.6  # ADX 15-20: developing trend

            # ATR as confidence modifier (not a hard gate)
            if low_volatility:
                confidence = max(0.45, confidence - 0.15)
                atr_note = " Low ATR — reduced confidence."
            else:
                atr_note = ""

            reasoning = f"SMA(9) crossed below EMA(21) with ADX at {current_adx:.1f} (trend confirmed).{atr_note}"

            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": round(confidence, 2),
                "reasoning": reasoning,
                "indicators": indicators
            }

        # No signal
        reasons = []
        if crossover == "none":
            reasons.append("no SMA/EMA crossover in last 5 bars")
        else:
            if not trend_exists:
                reasons.append(f"ADX too low ({current_adx:.1f} < 15)")

        reasoning = "HOLD: " + ", ".join(reasons) if reasons else "HOLD: No trend conditions met."

        return {
            "model": self.name,
            "vote": "HOLD",
            "confidence": 0.5,
            "reasoning": reasoning,
            "indicators": indicators
        }
    
    def _detect_crossover_last_n(self, fast_line: np.ndarray, slow_line: np.ndarray, n: int = 3) -> str:
        """Detect if fast line crossed slow line within last n bars."""
        if len(fast_line) < n + 1 or len(slow_line) < n + 1:
            return "none"
        
        for i in range(1, n + 1):
            curr_idx = -i
            prev_idx = -i - 1
            
            if abs(prev_idx) > len(fast_line) or abs(prev_idx) > len(slow_line):
                break
            
            fast_prev = fast_line[prev_idx]
            fast_curr = fast_line[curr_idx]
            slow_prev = slow_line[prev_idx]
            slow_curr = slow_line[curr_idx]
            
            # Bullish crossover: fast was below, now above
            if fast_prev < slow_prev and fast_curr > slow_curr:
                return "bullish"
            
            # Bearish crossover: fast was above, now below
            if fast_prev > slow_prev and fast_curr < slow_curr:
                return "bearish"
        
        return "none"
