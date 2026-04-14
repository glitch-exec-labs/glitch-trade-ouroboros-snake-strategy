"""
GlitchExecutor Model: Mamba Reversion
M15 Bollinger Band mean reversion in ranging markets.
Second best from Windows VM bots: 63.6% WR, +1794 PnL (best raw PnL).

Strategy:
  - Regime filter: ADX < 25 = ranging → TRADE, else SIT OUT
  - BB Lower Fade: price at/below lower BB + RSI oversold → BUY
  - BB Upper Fade: price at/above upper BB + RSI overbought → SELL
  - Two-tier confirmation: exact band touch = strict RSI, near-band = mild RSI
  Exits: TP at BB midline (SMA), SL beyond band + ATR buffer
"""
import numpy as np
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import atr, rsi, adx, bollinger_bands


_DEFAULT_CFG = {
    'bb_period': 20,
    'bb_std_mult': 2.0,
    'rsi_period': 14,
    'atr_period': 14,
    'adx_period': 14,
    'adx_threshold': 25,
    'rsi_oversold': 30,
    'rsi_overbought': 70,
    'bb_entry_pct': 0.15,
}


class MambaReversionModel(BaseModel):
    """Bollinger Band mean reversion model for ranging markets."""

    name = "mamba_reversion"
    version = "1.0"

    def __init__(self, config: dict = None):
        self.cfg = {**_DEFAULT_CFG, **(config or {})}

    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        m15 = candles.get("m15")
        if m15 is None or len(m15) < 60:
            return self._hold("Insufficient M15 data for BB/ADX calculation.")

        closes = m15[:, 4].astype(float)
        highs = m15[:, 2].astype(float)
        lows = m15[:, 3].astype(float)

        min_bars = max(self.cfg['bb_period'], self.cfg['adx_period'] * 2 + 5, self.cfg['rsi_period']) + 10
        if len(closes) < min_bars:
            return self._hold("Not enough bars for indicator warm-up.")

        atr_vals = atr(highs, lows, closes, self.cfg['atr_period'])
        curr_atr = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
        if curr_atr <= 0:
            return self._hold("ATR is zero.")

        adx_vals = adx(highs, lows, closes, self.cfg['adx_period'])
        curr_adx = float(adx_vals[-1])

        bb_upper, bb_mid, bb_lower = bollinger_bands(closes, self.cfg['bb_period'], self.cfg['bb_std_mult'])
        upper = float(bb_upper[-1])
        mid = float(bb_mid[-1])
        lower = float(bb_lower[-1])

        rsi_vals = rsi(closes, self.cfg['rsi_period'])
        curr_rsi = float(rsi_vals[-2]) if len(rsi_vals) > 1 and not np.isnan(rsi_vals[-2]) else 50.0
        price = float(closes[-2])

        bb_range = upper - lower
        price_pos = (price - lower) / bb_range if bb_range > 0 else 0.5

        base_indicators = {
            "bb_upper": round(upper, 5), "bb_mid": round(mid, 5), "bb_lower": round(lower, 5),
            "bb_width": round(bb_range, 5), "price_position_in_bb": round(price_pos, 3),
            "adx": round(curr_adx, 2), "rsi": round(curr_rsi, 2), "atr": round(curr_atr, 5),
            "regime": "ranging" if curr_adx < self.cfg['adx_threshold'] else "trending",
        }

        if curr_adx >= self.cfg['adx_threshold']:
            return self._hold(
                f"Market trending (ADX={curr_adx:.1f} >= {self.cfg['adx_threshold']}). Mean reversion inactive.",
                indicators=base_indicators)

        proximity = bb_range * self.cfg['bb_entry_pct']

        # Lower BB fade → BUY
        if price <= lower + proximity:
            at_band = price <= lower
            rsi_ok = (curr_rsi <= self.cfg['rsi_oversold']) if at_band else (curr_rsi < 50)
            if rsi_ok:
                trigger = 'BB_LOWER_FADE' if at_band else 'BB_LOWER_APPROACH'
                confidence = 0.70 if at_band else 0.60
                base_indicators["trigger"] = trigger
                base_indicators["suggested_sl"] = round(lower - curr_atr, 5)
                base_indicators["suggested_tp"] = round(mid, 5)
                return {
                    "model": self.name, "vote": "BUY", "confidence": confidence,
                    "reasoning": f"{'At' if at_band else 'Near'} lower BB: price {price:.5f} {'<=' if at_band else 'near'} {lower:.5f} ({price_pos*100:.0f}%), RSI={curr_rsi:.1f}, ADX={curr_adx:.1f}",
                    "indicators": base_indicators,
                }

        # Upper BB fade → SELL
        if price >= upper - proximity:
            at_band = price >= upper
            rsi_ok = (curr_rsi >= self.cfg['rsi_overbought']) if at_band else (curr_rsi > 50)
            if rsi_ok:
                trigger = 'BB_UPPER_FADE' if at_band else 'BB_UPPER_APPROACH'
                confidence = 0.70 if at_band else 0.60
                base_indicators["trigger"] = trigger
                base_indicators["suggested_sl"] = round(upper + curr_atr, 5)
                base_indicators["suggested_tp"] = round(mid, 5)
                return {
                    "model": self.name, "vote": "SELL", "confidence": confidence,
                    "reasoning": f"{'At' if at_band else 'Near'} upper BB: price {price:.5f} {'>=' if at_band else 'near'} {upper:.5f} ({price_pos*100:.0f}%), RSI={curr_rsi:.1f}, ADX={curr_adx:.1f}",
                    "indicators": base_indicators,
                }

        return self._hold(f"Ranging but price mid-band ({price_pos*100:.0f}%). Waiting for BB extremes.", indicators=base_indicators)

    def _hold(self, reasoning, indicators=None):
        return {"model": self.name, "vote": "HOLD", "confidence": 0.5, "reasoning": reasoning, "indicators": indicators or {}}
