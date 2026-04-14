"""
Dataclasses used across the collector.

All timestamps are UTC (timezone-aware). Indicator dicts are stored as
native Python types so json.dumps() works without custom encoders.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Signal:
    """One model evaluation at one point in time."""
    timestamp: datetime
    strategy: str          # e.g. "king_cobra" — collector-level name
    model_name: str        # e.g. "momentum_hunter" — model class name
    symbol: str
    timeframe: str         # "m15" (the bar frame the models evaluate on)
    vote: str              # BUY | SELL | HOLD
    confidence: float
    reasoning: str
    indicators: Dict[str, Any] = field(default_factory=dict)
    bar_open: float = 0.0
    bar_high: float = 0.0
    bar_low: float = 0.0
    bar_close: float = 0.0

    def to_row_dict(self) -> Dict[str, Any]:
        """Return a dict shaped for the CSV schema (flat)."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            "symbol": self.symbol,
            "bot": self.strategy,
            "timeframe": self.timeframe,
            "signal": self.vote,
            "signal_type": self.model_name,
            "confidence": round(float(self.confidence), 4),
            "reasoning": self.reasoning,
            "bar_open": round(float(self.bar_open), 6),
            "bar_high": round(float(self.bar_high), 6),
            "bar_low": round(float(self.bar_low), 6),
            "bar_close": round(float(self.bar_close), 6),
        }


@dataclass
class OpenTrade:
    """A trade that is currently open on a demo account, tracked to closure."""
    row_id: str            # UUID linking back to the CSV row
    strategy: str
    symbol: str
    account_id: int        # which demo account holds this position
    side: str              # BUY or SELL
    entry_price: float
    sl_price: float
    tp_price: float
    volume_lots: float
    ticket: str            # cTrader positionId (string, unique within account)
    opened_at: datetime
    signal_confidence: float
    signal_indicators: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        return d

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "OpenTrade":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        return cls(**d)


@dataclass
class ClosureEvent:
    """Detected when a tracked position is no longer in cTrader's reconcile list."""
    row_id: str
    strategy: str
    exit_price: float
    exit_reason: str       # tp_hit | sl_hit | manual_or_unknown
    profit: float
    pnl: float
    outcome: str           # WIN | LOSS | UNKNOWN
    duration_minutes: float
    account_balance: float
    account_equity: float

    def as_update(self) -> Dict[str, Any]:
        return {
            "exit_price": round(self.exit_price, 6),
            "exit_reason": self.exit_reason,
            "profit": round(self.profit, 4),
            "pnl": round(self.pnl, 4),
            "outcome": self.outcome,
            "duration_minutes": round(self.duration_minutes, 2),
            "account_balance": round(self.account_balance, 2),
            "account_equity": round(self.account_equity, 2),
        }
