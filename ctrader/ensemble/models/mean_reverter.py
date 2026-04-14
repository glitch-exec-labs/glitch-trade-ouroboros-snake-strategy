"""
GlitchExecutor Model 2: Mean Reverter
Uses Bollinger Bands + RSI for mean reversion signals in ranging markets.
"""
import numpy as np
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import bollinger_bands, rsi, adx


class MeanReverterModel(BaseModel):
    """
    Mean reversion strategy using:
    - Price outside Bollinger Bands (20, 2.0) — primary signal
    - RSI confirmation (< 35 oversold, > 65 overbought)
    - ADX < 30 confirming ranging market (not trending)
    - Secondary signal: price near BB band (within 0.3x width) with mild RSI
    """
    
    name = "mean_reverter"
    version = "1.0"
    
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Run mean reversion analysis on H1 candles."""
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
        upper_bb, middle_bb, lower_bb = bollinger_bands(closes, 20, 2.0)
        rsi_vals = rsi(closes, 14)
        adx_vals = adx(highs, lows, closes, 14)
        
        # Get current values
        current_close = closes[-1]
        current_upper = upper_bb[-1]
        current_lower = lower_bb[-1]
        current_rsi = rsi_vals[-1] if not np.isnan(rsi_vals[-1]) else 50
        current_adx = adx_vals[-1] if not np.isnan(adx_vals[-1]) else 0
        
        # Build indicators dict
        indicators = {
            "close": round(float(current_close), 4),
            "bb_upper": round(float(current_upper), 4),
            "bb_lower": round(float(current_lower), 4),
            "rsi": round(float(current_rsi), 2),
            "adx": round(float(current_adx), 2),
            "bb_position": "below" if current_close < current_lower else "above" if current_close > current_upper else "inside"
        }
        
        # Check ranging market condition (widened from 25 to 30)
        is_ranging = current_adx < 30

        # BB band width for proximity calculations
        bb_width = current_upper - current_lower if (current_upper - current_lower) > 0 else 0.0001

        # Check for oversold (potential BUY) — widened RSI from 30 to 35
        is_below_bb = current_close < current_lower
        is_oversold = current_rsi < 35

        # Check for overbought (potential SELL) — widened RSI from 70 to 65
        is_above_bb = current_close > current_upper
        is_overbought = current_rsi > 65

        # Secondary signal: price near BB band (within 0.3x width) with mild RSI
        near_lower = current_close < (current_lower + bb_width * 0.3)
        near_upper = current_close > (current_upper - bb_width * 0.3)
        mild_oversold = current_rsi < 42  # not extreme, but leaning oversold
        mild_overbought = current_rsi > 58  # not extreme, but leaning overbought

        # Generate signals if ranging
        if is_ranging:
            # Primary signal: price outside BB + RSI extreme
            if is_below_bb and is_oversold:
                rsi_extreme = max(0, (35 - current_rsi) / 35)
                bb_distance = (current_lower - current_close) / bb_width
                confidence = 0.6 + (rsi_extreme * 0.2) + (min(bb_distance, 0.2) * 0.5)
                confidence = min(0.95, confidence)

                reasoning = f"Price below lower BB ({current_close:.2f} < {current_lower:.2f}) with RSI oversold at {current_rsi:.1f} in ranging market (ADX {current_adx:.1f})."

                return {
                    "model": self.name,
                    "vote": "BUY",
                    "confidence": round(confidence, 2),
                    "reasoning": reasoning,
                    "indicators": indicators
                }

            elif is_above_bb and is_overbought:
                rsi_extreme = max(0, (current_rsi - 65) / 35)
                bb_distance = (current_close - current_upper) / bb_width
                confidence = 0.6 + (rsi_extreme * 0.2) + (min(bb_distance, 0.2) * 0.5)
                confidence = min(0.95, confidence)

                reasoning = f"Price above upper BB ({current_close:.2f} > {current_upper:.2f}) with RSI overbought at {current_rsi:.1f} in ranging market (ADX {current_adx:.1f})."

                return {
                    "model": self.name,
                    "vote": "SELL",
                    "confidence": round(confidence, 2),
                    "reasoning": reasoning,
                    "indicators": indicators
                }

            # Secondary signal: price near BB + mild RSI (weaker signal)
            elif near_lower and mild_oversold and not is_above_bb:
                confidence = 0.55
                reasoning = f"Price approaching lower BB ({current_close:.2f} near {current_lower:.2f}) with RSI leaning oversold at {current_rsi:.1f}. Weaker mean reversion signal."

                return {
                    "model": self.name,
                    "vote": "BUY",
                    "confidence": round(confidence, 2),
                    "reasoning": reasoning,
                    "indicators": indicators
                }

            elif near_upper and mild_overbought and not is_below_bb:
                confidence = 0.55
                reasoning = f"Price approaching upper BB ({current_close:.2f} near {current_upper:.2f}) with RSI leaning overbought at {current_rsi:.1f}. Weaker mean reversion signal."

                return {
                    "model": self.name,
                    "vote": "SELL",
                    "confidence": round(confidence, 2),
                    "reasoning": reasoning,
                    "indicators": indicators
                }

        # No signal
        reasons = []
        if not is_ranging:
            reasons.append(f"trending market (ADX {current_adx:.1f})")
        if not is_below_bb and not is_above_bb and not near_lower and not near_upper:
            reasons.append("price within BB bands")
        if is_below_bb and not is_oversold:
            reasons.append(f"RSI not oversold ({current_rsi:.1f})")
        if is_above_bb and not is_overbought:
            reasons.append(f"RSI not overbought ({current_rsi:.1f})")

        reasoning = "HOLD: " + ", ".join(reasons) if reasons else "HOLD: No mean reversion conditions met."

        return {
            "model": self.name,
            "vote": "HOLD",
            "confidence": 0.5,
            "reasoning": reasoning,
            "indicators": indicators
        }
