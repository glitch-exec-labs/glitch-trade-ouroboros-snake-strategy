"""
News-event embargo layer for Ouroboros.

Polls newsdata.io every ML_NEWSGUARD_INTERVAL_SECONDS (default 600 = 10 min),
matches each article against ml_news_rules (ILIKE patterns), persists matches
to ml_news_events with embargo_until = published_at + rule.embargo_minutes.

Oracle.check_trade_allowed consults ml_news_events.embargo_until to gate
affected symbols/buckets during the embargo window.

Requires NEWSDATA_API_KEY in the runtime .env. Free tier has no delay on
basic headline data; it does not include content body or sentiment (paid).

Run: python -m ml_collector.news_guard
     (or wire news_guard_loop into collector.py as a sibling asyncio task)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import asyncpg

from .config import get_config, configure_logging

logger = logging.getLogger("ml_collector.news_guard")

NEWSDATA_BASE = "https://newsdata.io/api/1/latest"

# How often to poll. Free tier has real-time headlines but a 200 req/day
# quota on the basic plan — 10 min intervals ~ 144 req/day, safe margin.
NEWSGUARD_INTERVAL_SEC = int(os.environ.get("ML_NEWSGUARD_INTERVAL_SECONDS", "600"))

# Keyword queries — broad enough to catch most macro/geopolitical events.
# newsdata.io lets us send multiple keywords in one call; we rotate through
# these on each poll to cover more ground without burning the quota.
NEWSGUARD_QUERIES = [
    "FOMC OR CPI OR NFP OR Fed",
    "ECB OR BOE OR BOJ OR \"rate decision\"",
    "war OR invasion OR sanctions OR OPEC",
    "inflation OR unemployment OR GDP",
]

_q_cursor = 0  # rotates through NEWSGUARD_QUERIES


# ── newsdata.io client ────────────────────────────────────────────────────────

async def _fetch_latest(session: aiohttp.ClientSession, apikey: str, q: str,
                        language: str = "en", size: int = 10) -> List[Dict[str, Any]]:
    params = {"apikey": apikey, "q": q, "language": language, "size": str(size)}
    try:
        async with session.get(NEWSDATA_BASE, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            if data.get("status") != "success":
                logger.warning("newsdata.io non-success: %s", data.get("message") or data)
                return []
            return data.get("results") or []
    except Exception:
        logger.exception("newsdata.io fetch failed (q=%r)", q)
        return []


# ── Rule matcher ──────────────────────────────────────────────────────────────

def _ilike_to_regex(pattern: str) -> re.Pattern:
    """Convert a '%term1%term2%' ILIKE-style pattern to a word-boundary regex.

    Uses \b word boundaries around each term so short keywords like "war" or
    "Fed" do not substring-match inside "Warwickshire" or "Federer". Terms
    are joined by .* so '%Fed%rate%' requires both tokens in order but with
    any text between.
    """
    terms = [t for t in pattern.split("%") if t.strip()]
    if not terms:
        return re.compile("(?!)")  # never matches
    parts = [rf"\b{re.escape(t)}\b" for t in terms]
    return re.compile(".*".join(parts), re.IGNORECASE | re.DOTALL)


async def _load_rules(pool: asyncpg.Pool) -> List[Dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT id, rule_name, pattern, event_type, impact, embargo_minutes, "
        "       affected_buckets, affected_symbols "
        "FROM ml_news_rules WHERE enabled = TRUE ORDER BY id"
    )
    out = []
    for r in rows:
        out.append({
            "id": int(r["id"]), "rule_name": r["rule_name"],
            "regex": _ilike_to_regex(r["pattern"]),
            "event_type": r["event_type"], "impact": r["impact"],
            "embargo_minutes": int(r["embargo_minutes"]),
            "affected_buckets": list(r["affected_buckets"] or []),
            "affected_symbols": list(r["affected_symbols"] or []),
        })
    return out


def _match_rule(rules: List[Dict], title: str, description: str) -> Optional[Dict]:
    """Return first matching rule, or None. Impact rank high > medium > low."""
    haystack = f"{title or ''} {description or ''}"
    best = None
    for r in rules:
        if r["regex"].search(haystack):
            if best is None or _impact_rank(r["impact"]) > _impact_rank(best["impact"]):
                best = r
    return best


def _impact_rank(impact: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(impact, 0)


# ── Persistence ───────────────────────────────────────────────────────────────

def _parse_pub_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # "2026-04-20 15:37:13" (newsdata.io always UTC)
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


async def _persist_article(pool: asyncpg.Pool, article: Dict[str, Any],
                           rule: Optional[Dict]) -> bool:
    """Insert with ON CONFLICT DO NOTHING. Returns True if a new row landed."""
    article_id = article.get("article_id")
    if not article_id:
        return False

    pub = _parse_pub_date(article.get("pubDate"))
    embargo_until = None
    event_type = impact = None
    buckets: List[str] = []
    symbols: List[str] = []
    rule_id = None

    if rule is not None:
        rule_id     = rule["id"]
        event_type  = rule["event_type"]
        impact      = rule["impact"]
        buckets     = rule["affected_buckets"]
        symbols     = rule["affected_symbols"]
        base_time   = pub or datetime.now(timezone.utc)
        candidate = base_time + timedelta(minutes=rule["embargo_minutes"])
        # Only set an embargo window if it\'s actually in the future.
        # Back-dated articles (hours-old pubDate) are still recorded for
        # historical context but should not block current trades.
        if candidate > datetime.now(timezone.utc):
            embargo_until = candidate

    try:
        row = await pool.fetchrow(
            """
            INSERT INTO ml_news_events (
                article_id, title, description, link, source, published_at,
                category, country,
                matched_rule_id, event_type, impact, affected_buckets, affected_symbols,
                embargo_until
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT (article_id) DO NOTHING
            RETURNING id
            """,
            article_id,
            (article.get("title") or "")[:4000],
            (article.get("description") or "")[:4000],
            article.get("link"),
            article.get("source_name") or article.get("source_id"),
            pub,
            article.get("category") or [],
            article.get("country") or [],
            rule_id, event_type, impact, buckets, symbols,
            embargo_until,
        )
        return row is not None
    except Exception:
        logger.exception("persist failed for article_id=%s", article_id)
        return False


# ── Public gate helper — called by Oracle ─────────────────────────────────────

async def active_embargoes_for(pool: asyncpg.Pool, symbol: str, bucket: Optional[str]
                               ) -> List[Dict[str, Any]]:
    """
    Return currently-active embargo events that affect this symbol or bucket.
    Used by oracle.check_trade_allowed to ABSTAIN during high-impact windows.
    """
    rows = await pool.fetch(
        """
        SELECT id, event_type, impact, embargo_until, title, affected_buckets, affected_symbols
        FROM ml_news_events
        WHERE embargo_until IS NOT NULL
          AND embargo_until > NOW()
          AND impact IN ('high','medium')
          AND (
               $1::text = ANY(affected_symbols)
            OR ($2::text IS NOT NULL AND $2::text = ANY(affected_buckets))
          )
        ORDER BY embargo_until DESC
        LIMIT 5
        """,
        symbol, bucket,
    )
    return [dict(r) for r in rows]


# ── Loop ──────────────────────────────────────────────────────────────────────

async def news_guard_loop(pool: asyncpg.Pool) -> None:
    global _q_cursor
    apikey = os.environ.get("NEWSDATA_API_KEY", "").strip()
    if not apikey:
        logger.error("NEWSDATA_API_KEY not set in .env — news_guard disabled")
        return

    logger.info("news_guard started (interval=%ds, %d queries rotating)",
                NEWSGUARD_INTERVAL_SEC, len(NEWSGUARD_QUERIES))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                rules = await _load_rules(pool)
                q = NEWSGUARD_QUERIES[_q_cursor % len(NEWSGUARD_QUERIES)]
                _q_cursor += 1

                articles = await _fetch_latest(session, apikey, q, size=10)
                new_rows = matched = 0
                for art in articles:
                    rule = _match_rule(rules, art.get("title") or "", art.get("description") or "")
                    if await _persist_article(pool, art, rule):
                        new_rows += 1
                        if rule:
                            matched += 1
                            logger.warning(
                                "NEWS EMBARGO %s impact=%s until=%s — %s",
                                rule["event_type"], rule["impact"],
                                (_parse_pub_date(art.get("pubDate")) or datetime.now(timezone.utc)
                                 ) + timedelta(minutes=rule["embargo_minutes"]),
                                (art.get("title") or "")[:120],
                            )
                logger.info("news_guard cycle q=%r new=%d matched=%d", q, new_rows, matched)

            except Exception:
                logger.exception("news_guard_loop iteration failed")

            await asyncio.sleep(NEWSGUARD_INTERVAL_SEC)


async def main() -> None:
    cfg = get_config()
    configure_logging(cfg)
    pool = await asyncpg.create_pool(cfg.db_dsn, min_size=1, max_size=4)
    try:
        await news_guard_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
