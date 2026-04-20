"""
Ouroboros Oracle — ensemble coordination layer.

Runs alongside the six per-bot signal loops. Every ORACLE_INTERVAL_SECONDS,
for each symbol in ML_SYMBOLS, it:

  1. Reads the most recent ml_signals row per bot within the bot's
     freshness window (from ml_oracle_weights.freshness_sec).
  2. Computes weighted BUY / SELL / HOLD scores using per-bot weights.
  3. Applies the regime gate (can_veto=TRUE bots voting HOLD with high
     confidence force ABSTAIN).
  4. Applies the exposure gate (portfolio-level open-trade cap across
     correlated symbols — currency-bucket based).
  5. Writes one ml_oracle_decisions row with the full contributor breakdown.

Shadow mode only — no trades are executed. Once decisions are calibrated
against real bot outcomes, a separate execution adapter can be wired in.

Run: python -m ml_collector.oracle
     (or add oracle_loop() to collector.py as a 7th asyncio task)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import asyncpg

from .config import get_config, configure_logging

logger = logging.getLogger("ml_collector.oracle")

# ── Tunables (overridable via env) ────────────────────────────────────────────
ORACLE_INTERVAL_SECONDS   = int(os.environ.get("ML_ORACLE_INTERVAL_SECONDS", "60"))
ORACLE_MIN_VOTERS         = int(os.environ.get("ML_ORACLE_MIN_VOTERS", "3"))
ORACLE_DECISION_THRESHOLD = float(os.environ.get("ML_ORACLE_DECISION_THRESHOLD", "0.55"))
ORACLE_VETO_CONF          = float(os.environ.get("ML_ORACLE_VETO_CONF", "0.80"))
ORACLE_MAX_EXPOSURE_PER_BUCKET = int(os.environ.get("ML_ORACLE_MAX_EXPOSURE_PER_BUCKET", "3"))
ORACLE_MODE               = os.environ.get("ML_ORACLE_MODE", "shadow")  # shadow | live

# Currency-bucket map used by the exposure gate. Symbols sharing a bucket
# correlate strongly and we cap aggregate open exposure across them.
CORRELATION_BUCKETS: Dict[str, str] = {
    "EURUSD": "USD_MAJOR", "GBPUSD": "USD_MAJOR", "AUDUSD": "USD_MAJOR",
    "NZDUSD": "USD_MAJOR", "USDJPY": "USD_MAJOR", "USDCHF": "USD_MAJOR",
    "USDCAD": "USD_MAJOR",
    "GBPJPY": "JPY_CROSS", "EURJPY": "JPY_CROSS",
    "XAUUSD": "METALS",    "XAGUSD": "METALS",
    "XTIUSD": "ENERGY",
    "US500":  "EQUITY_US", "US100":  "EQUITY_US",
    "GER40":  "EQUITY_EU", "UK100":  "EQUITY_EU",
    "JPN225": "EQUITY_AS",
}


@dataclass(frozen=True)
class BotWeight:
    bot_name: str
    weight: float
    can_veto: bool
    freshness_sec: int


@dataclass
class BotVote:
    bot_name: str
    vote: str              # BUY / SELL / HOLD
    confidence: float
    weight: float
    age_sec: int


# ── Core rule engine ──────────────────────────────────────────────────────────

def score_votes(votes: List[BotVote]) -> Tuple[float, float, float]:
    """Return (buy_score, sell_score, hold_score) as weighted confidence sums."""
    buy = sell = hold = 0.0
    for v in votes:
        w = v.weight * v.confidence
        if v.vote == "BUY":
            buy += w
        elif v.vote == "SELL":
            sell += w
        else:  # HOLD or unknown
            hold += w
    return buy, sell, hold


def resolve_decision(
    votes: List[BotVote],
    buy: float, sell: float, hold: float,
) -> Tuple[str, float, Optional[str]]:
    """
    Return (decision, confidence, abstain_reason).

    decision ∈ {BUY, SELL, HOLD, ABSTAIN}. ABSTAIN is reserved for
    veto / exposure / insufficient-consensus gating; HOLD means the
    ensemble genuinely thinks nothing to do.
    """
    if len(votes) < ORACLE_MIN_VOTERS:
        return "ABSTAIN", 0.0, "insufficient_voters"

    # Regime veto: any can_veto bot voting HOLD with high conf forces ABSTAIN
    for v in votes:
        if v.bot_name in _VETO_BOTS and v.vote == "HOLD" and v.confidence >= ORACLE_VETO_CONF:
            return "ABSTAIN", 0.0, f"regime_veto:{v.bot_name}"

    total = buy + sell + hold
    if total <= 0:
        return "HOLD", 0.0, None

    top_score = max(buy, sell, hold)
    share     = top_score / total
    if share < ORACLE_DECISION_THRESHOLD:
        return "HOLD", share, "no_consensus"

    if top_score == buy:   return "BUY",  share, None
    if top_score == sell:  return "SELL", share, None
    return "HOLD", share, None


# Populated from ml_oracle_weights at startup.
_VETO_BOTS: set[str] = set()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _load_weights(pool: asyncpg.Pool) -> Dict[str, BotWeight]:
    rows = await pool.fetch("SELECT bot_name, weight, can_veto, freshness_sec FROM ml_oracle_weights")
    out: Dict[str, BotWeight] = {}
    for r in rows:
        bw = BotWeight(r["bot_name"], float(r["weight"]), bool(r["can_veto"]), int(r["freshness_sec"]))
        out[bw.bot_name] = bw
        if bw.can_veto:
            _VETO_BOTS.add(bw.bot_name)
    return out


async def _latest_signals(
    pool: asyncpg.Pool, symbol: str, weights: Dict[str, BotWeight],
) -> List[BotVote]:
    """Latest signal per bot for this symbol within each bot's freshness window."""
    # One query: rank by created_at DESC per bot, pick row 1, filter by per-bot freshness.
    rows = await pool.fetch(
        """
        SELECT bot_name, vote, confidence,
               EXTRACT(EPOCH FROM (NOW() - created_at))::int AS age_sec
        FROM (
            SELECT bot_name, vote, confidence, created_at,
                   ROW_NUMBER() OVER (PARTITION BY bot_name ORDER BY created_at DESC) AS rn
            FROM ml_signals
            WHERE symbol = $1 AND created_at > NOW() - INTERVAL '24 hours'
        ) t
        WHERE rn = 1
        """,
        symbol,
    )
    votes: List[BotVote] = []
    for r in rows:
        bot = r["bot_name"]
        bw = weights.get(bot)
        if bw is None:
            continue  # unknown bot, ignore
        age = int(r["age_sec"])
        if age > bw.freshness_sec:
            continue  # stale
        votes.append(BotVote(
            bot_name=bot,
            vote=str(r["vote"]),
            confidence=float(r["confidence"]),
            weight=bw.weight,
            age_sec=age,
        ))
    return votes


async def _bucket_exposure(pool: asyncpg.Pool, bucket: Optional[str]) -> int:
    """Open ml_trades count across all symbols in the same correlation bucket."""
    if not bucket:
        return 0
    same_bucket = [s for s, b in CORRELATION_BUCKETS.items() if b == bucket]
    if not same_bucket:
        return 0
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS n FROM ml_trades WHERE closed_at IS NULL AND symbol = ANY($1::text[])",
        same_bucket,
    )
    return int(row["n"])


async def _write_decision(
    pool: asyncpg.Pool, symbol: str,
    decision: str, confidence: float,
    buy: float, sell: float, hold: float,
    votes: List[BotVote], abstain_reason: Optional[str],
) -> None:
    contributors = {
        v.bot_name: {
            "vote": v.vote,
            "conf": round(v.confidence, 4),
            "weight": v.weight,
            "age_sec": v.age_sec,
        }
        for v in votes
    }
    await pool.execute(
        """
        INSERT INTO ml_oracle_decisions
            (symbol, decision, decision_confidence,
             buy_score, sell_score, hold_score,
             contributors, abstain_reason, mode)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
        """,
        symbol, decision, round(confidence, 4),
        round(buy, 4), round(sell, 4), round(hold, 4),
        json.dumps(contributors), abstain_reason, ORACLE_MODE,
    )


# ── Loop ──────────────────────────────────────────────────────────────────────

async def oracle_loop(pool: asyncpg.Pool) -> None:
    cfg = get_config()
    weights = await _load_weights(pool)
    logger.info("Oracle started (mode=%s, %d symbols, weights=%s)",
                ORACLE_MODE, len(cfg.symbols), {k: v.weight for k, v in weights.items()})

    while True:
        try:
            for symbol in cfg.symbols:
                votes = await _latest_signals(pool, symbol, weights)

                if not votes:
                    await _write_decision(pool, symbol, "ABSTAIN", 0.0,
                                          0.0, 0.0, 0.0, [], "no_signals")
                    continue

                buy, sell, hold = score_votes(votes)
                decision, conf, reason = resolve_decision(votes, buy, sell, hold)

                # Exposure gate — only check if we'd otherwise act.
                if decision in ("BUY", "SELL"):
                    exposure = await _bucket_exposure(pool, CORRELATION_BUCKETS.get(symbol))
                    if exposure >= ORACLE_MAX_EXPOSURE_PER_BUCKET:
                        decision = "ABSTAIN"
                        reason = f"exposure_cap:{exposure}"

                await _write_decision(pool, symbol, decision, conf,
                                      buy, sell, hold, votes, reason)

                logger.info(
                    "Oracle %s → %s (conf=%.3f, buy=%.3f sell=%.3f hold=%.3f, voters=%d%s)",
                    symbol, decision, conf, buy, sell, hold, len(votes),
                    f", reason={reason}" if reason else "",
                )

        except Exception:
            logger.exception("oracle_loop iteration failed")

        await asyncio.sleep(ORACLE_INTERVAL_SECONDS)


async def main() -> None:
    cfg = get_config()
    configure_logging(cfg)
    pool = await asyncpg.create_pool(cfg.db_dsn, min_size=1, max_size=4)
    try:
        await oracle_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
