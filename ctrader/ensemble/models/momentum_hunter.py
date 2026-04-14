"""
GlitchExecutor Model 3: Momentum Hunter
Uses RSI momentum breaks with EMA trend filter and volume confirmation.
"""
import numpy as np
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import rsi, ema


class MomentumHunterModel(BaseModel):
    """
    Momentum strategy using:
    - RSI(14) crossing above 52 (bullish) or below 48 (bearish) within last 5 bars
    - Price vs EMA(20) as confidence modifier (not a hard gate)
    - Volume > 1.3x average for confidence boost
    """
    
    name = "momentum_hunter"
    version = "1.0"
    
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Run momentum analysis on M15 candles."""
        m15_candles = candles.get("m15")
        
        if m15_candles is None or len(m15_candles) < 50:
            return {
                "model": self.name,
                "vote": "HOLD",
                "confidence": 0.0,
                "reasoning": "Insufficient M15 candle data for analysis.",
                "indicators": {}
            }
        
        # Extract OHLCV
        _, highs, lows, closes, volumes = self._extract_ohlcv(m15_candles)
        
        if closes is None or len(closes) < 50:
            return {
                "model": self.name,
                "vote": "HOLD",
                "confidence": 0.0,
                "reasoning": "Invalid close price data.",
                "indicators": {}
            }
        
        # Calculate indicators
        rsi_vals = rsi(closes, 14)
        ema_20 = ema(closes, 20)
        
        # Get current values
        current_close = closes[-1]
        current_rsi = rsi_vals[-1] if not np.isnan(rsi_vals[-1]) else 50
        current_ema = ema_20[-1] if len(ema_20) > 0 else current_close
        current_volume = volumes[-1] if volumes is not None else 0
        
        # Calculate volume average (50 bars)
        vol_avg = np.mean(volumes[-50:]) if volumes is not None and len(volumes) >= 50 else current_volume
        volume_ratio = current_volume / vol_avg if vol_avg > 0 else 1.0
        
        # Detect RSI crossover within last 5 bars
        rsi_crossover = self._detect_rsi_crossover(rsi_vals, 5)
        
        # Build indicators dict
        indicators = {
            "rsi": round(float(current_rsi), 2),
            "ema_20": round(float(current_ema), 4),
            "price_above_ema": current_close > current_ema,
            "current_volume": round(float(current_volume), 2),
            "volume_avg_50": round(float(vol_avg), 2),
            "volume_ratio": round(float(volume_ratio), 2),
            "rsi_crossover": rsi_crossover
        }
        
        # Check conditions — EMA as confidence modifier, not hard gate
        price_above_ema = current_close > current_ema
        price_below_ema = current_close < current_ema
        volume_confirmed = volume_ratio > 1.3

        # Generate signals — RSI crossover is primary, EMA confirms
        if rsi_crossover == "bullish":
            # Bullish momentum detected
            confidence = 0.65  # Base confidence for RSI break
            notes = []

            # EMA alignment boosts confidence (no longer a gate)
            if price_above_ema:
                confidence += 0.1
                notes.append("price above EMA(20)")
            else:
                notes.append("price below EMA(20) — reduced confidence")

            # Volume boost
            if volume_confirmed:
                confidence += 0.15
                notes.append("volume confirmed")

            confidence = min(0.95, confidence)
            reasoning = f"RSI broke above 52 — momentum shift to bullish. {', '.join(notes)}."

            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": round(confidence, 2),
                "reasoning": reasoning,
                "indicators": indicators
            }

        elif rsi_crossover == "bearish":
            # Bearish momentum detected
            confidence = 0.65  # Base confidence for RSI break
            notes = []

            # EMA alignment boosts confidence (no longer a gate)
            if price_below_ema:
                confidence += 0.1
                notes.append("price below EMA(20)")
            else:
                notes.append("price above EMA(20) — reduced confidence")

            # Volume boost
            if volume_confirmed:
                confidence += 0.15
                notes.append("volume confirmed")

            confidence = min(0.95, confidence)
            reasoning = f"RSI broke below 48 — momentum shift to bearish. {', '.join(notes)}."

            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": round(confidence, 2),
                "reasoning": reasoning,
                "indicators": indicators
            }

        # No RSI crossover detected
        reasoning = "HOLD: no RSI momentum break in last 5 bars."

        return {
            "model": self.name,
            "vote": "HOLD",
            "confidence": 0.5,
            "reasoning": reasoning,
            "indicators": indicators
        }
    
    def _detect_rsi_crossover(self, rsi_vals: np.ndarray, n: int = 5) -> str:
        """Detect if RSI crossed above 52 or below 48 within last n bars."""
        if len(rsi_vals) < n + 1 or np.all(np.isnan(rsi_vals)):
            return "none"
        
        # Get last n+1 valid RSI values
        valid_rsi = rsi_vals[~np.isnan(rsi_vals)]
        if len(valid_rsi) < n + 1:
            return "none"
        
        recent_rsi = valid_rsi[-(n+1):]
        
        for i in range(1, len(recent_rsi)):
            prev_rsi = recent_rsi[i-1]
            curr_rsi = recent_rsi[i]
            
            # Bullish: crossed above 52
            if prev_rsi <= 52 and curr_rsi > 52:
                return "bullish"

            # Bearish: crossed below 48
            if prev_rsi >= 48 and curr_rsi < 48:
                return "bearish"
        
        return "none"
