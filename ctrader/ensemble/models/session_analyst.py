"""
GlitchExecutor Model 7: Session Analyst
Analyzes trading session quality and adjusts confidence based on market hours.
"""
import numpy as np
from datetime import datetime
from typing import Dict, Any
from .base_model import BaseModel
from .indicators import ema


class SessionAnalystModel(BaseModel):
    """
    Trading session analysis:
    - London session: 7-16 UTC
    - New York session: 12-21 UTC
    - Overlap: 12-16 UTC (strongest)
    - Asian session: 0-7 UTC (weakest for forex — low confidence, not blocked)
    - All sessions can produce signals, confidence varies by session quality
    """
    
    name = "session_analyst"
    version = "1.0"
    
    # Forex symbols that should avoid Asian session
    FOREX_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USOUSD", "AUDUSD", "USDCAD"]
    
    def analyze(self, symbol: str, candles: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Analyze current session and return bias based on EMA direction."""
        
        # Get current UTC time
        now = datetime.utcnow()
        hour_utc = now.hour
        
        # Determine session
        is_asian = 0 <= hour_utc < 7
        is_london = 7 <= hour_utc < 16
        is_ny = 12 <= hour_utc < 21
        is_overlap = 12 <= hour_utc < 16  # London + NY overlap
        
        # Determine symbol type
        symbol_upper = symbol.upper()
        is_forex = symbol_upper in self.FOREX_SYMBOLS
        is_crypto = not is_forex  # Assume crypto if not forex
        
        # Get trend direction from H1 EMA
        h1_candles = candles.get("h1")
        ema_direction = self._get_ema_direction(h1_candles)
        
        # Build indicators
        indicators = {
            "hour_utc": hour_utc,
            "is_asian": is_asian,
            "is_london": is_london,
            "is_ny": is_ny,
            "is_overlap": is_overlap,
            "is_forex": is_forex,
            "is_crypto": is_crypto,
            "ema_direction": ema_direction
        }
        
        # Session quality assessment
        if is_overlap:
            session_quality = "excellent"
            base_confidence = 0.9
        elif is_london or is_ny:
            session_quality = "good"
            base_confidence = 0.8
        elif is_asian:
            session_quality = "poor" if is_forex else "fair"
            base_confidence = 0.5 if is_forex else 0.7
        else:
            session_quality = "closed"
            base_confidence = 0.5
        
        indicators["session_quality"] = session_quality
        
        # Generate signal based on EMA direction — all sessions can signal
        if ema_direction == "rising":
            confidence = base_confidence
            if is_asian and is_crypto:
                confidence *= 0.9  # Slight reduction for crypto in Asian

            # Determine session description
            if is_overlap:
                session_desc = "overlap"
            elif is_london:
                session_desc = "London"
            elif is_ny:
                session_desc = "NY"
            elif is_asian:
                session_desc = "Asian"
            else:
                session_desc = "off-hours"

            asian_note = " Lower liquidity — reduced confidence." if (is_forex and is_asian) else ""

            return {
                "model": self.name,
                "vote": "BUY",
                "confidence": round(confidence, 2),
                "reasoning": f"{session_desc} session active with {session_quality} conditions. EMA rising confirms bullish bias.{asian_note}",
                "indicators": indicators
            }

        elif ema_direction == "falling":
            confidence = base_confidence
            if is_asian and is_crypto:
                confidence *= 0.9  # Slight reduction for crypto in Asian

            if is_overlap:
                session_desc = "overlap"
            elif is_london:
                session_desc = "London"
            elif is_ny:
                session_desc = "NY"
            elif is_asian:
                session_desc = "Asian"
            else:
                session_desc = "off-hours"

            asian_note = " Lower liquidity — reduced confidence." if (is_forex and is_asian) else ""

            return {
                "model": self.name,
                "vote": "SELL",
                "confidence": round(confidence, 2),
                "reasoning": f"{session_desc} session active with {session_quality} conditions. EMA falling confirms bearish bias.{asian_note}",
                "indicators": indicators
            }
        
        # Flat EMA — no directional bias
        return {
            "model": self.name,
            "vote": "HOLD",
            "confidence": 0.5,
            "reasoning": f"Session conditions {session_quality} but flat EMA — no directional bias.",
            "indicators": indicators
        }
    
    def _get_ema_direction(self, candles: np.ndarray) -> str:
        """Get EMA(20) direction from H1 candles."""
        if candles is None or len(candles) < 30:
            return "flat"
        
        closes = candles[:, 4] if candles.shape[1] > 4 else None
        if closes is None or len(closes) < 30:
            return "flat"
        
        ema_20 = ema(closes, 20)
        if len(ema_20) < 10:
            return "flat"
        
        # Compare recent EMA values
        ema_recent = ema_20[-5:]
        ema_change = ema_recent[-1] - ema_recent[0]
        
        threshold = ema_recent[-1] * 0.0001  # 0.01% threshold
        
        if ema_change > threshold:
            return "rising"
        elif ema_change < -threshold:
            return "falling"
        return "flat"
