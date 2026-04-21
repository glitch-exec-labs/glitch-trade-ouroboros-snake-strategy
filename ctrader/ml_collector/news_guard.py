"""
News-event embargo cache for Ouroboros.

Architecture: a cron-driven refresher (every ~5h) fetches fresh headlines
from newsdata.io, asks an LLM to classify each new article for trading
impact, and persists the classification plus an embargo_until timestamp.
Oracle.check_trade_allowed reads the cached ml_news_events rows on every
trade attempt (DB-local, cheap) — it never calls the external news API
in the hot path.

Why cron instead of an in-process loop:
  - newsdata.io free tier: 200 req/day quota; 5h cadence = ~5 req/day, tiny
  - Claude classification cost: small (one short prompt per new article)
  - Most market-moving macro events (CPI, FOMC, NFP) are on a schedule
    known hours in advance; a 5h refresh window catches them before
    liquidity repositioning fully starts.
  - Unscheduled geopolitical shocks get caught at the next refresh
    (worst-case 5h lag). Trade-off explicitly accepted by the operator.

Run:
    python -m ml_collector.news_guard  # bootstrap refresh (systemd timer fires this)
    python -m ml_collector.news_guard --dry-run  # fetch + classify, don't persist

Oracle's active_embargoes_for() is the hot-path query used on every trade.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg

from .config import get_config, configure_logging

logger = logging.getLogger("ml_collector.news_guard")

NEWSDATA_BASE = "https://newsdata.io/api/1/latest"

# Queries rotated across refreshes to cover different event categories.
# Free tier returns ~10 articles per call; 4 queries = ~40 articles per refresh.
NEWSDATA_QUERIES = [
    "FOMC OR CPI OR NFP OR Fed OR Powell",
    "ECB OR BOE OR BOJ OR \"rate decision\" OR Lagarde",
    "war OR invasion OR sanctions OR OPEC OR \"oil supply\"",
    "inflation OR unemployment OR GDP OR recession",
]

# Claude model — use Haiku for cost. One short prompt per new article.
CLASSIFIER_MODEL = os.environ.get("ML_NEWS_CLASSIFIER_MODEL", "gpt-4o-mini")
MAX_CLASSIFY_PER_RUN = int(os.environ.get("ML_NEWS_MAX_CLASSIFY_PER_RUN", "40"))

# The correlation buckets the classifier may reference. Must match
# oracle.CORRELATION_BUCKETS.
ALLOWED_BUCKETS = [
    "USD_MAJOR", "JPY_CROSS", "METALS", "ENERGY",
    "EQUITY_US", "EQUITY_EU", "EQUITY_AS",
]

CLASSIFIER_SYSTEM = """You are a risk classifier for a systematic trading system that runs forex, metals, energy, and equity-index strategies.

Given a news headline and description, decide whether the article represents a MARKET-MOVING event that should temporarily block new trades on affected asset classes. Return ONLY a single JSON object with this exact shape:

{
  "impact": "high" | "medium" | "low" | "none",
  "event_type": "<short snake_case tag, e.g. 'us_cpi', 'fomc', 'ecb_rate', 'oil_supply_shock', 'geopolitical', 'earnings', 'noise'>",
  "affected_buckets": [<subset of: "USD_MAJOR", "JPY_CROSS", "METALS", "ENERGY", "EQUITY_US", "EQUITY_EU", "EQUITY_AS">],
  "affected_symbols": [<optional list of specific tickers like "EURUSD", "XAUUSD", "US500">],
  "embargo_minutes": <integer, how long after the event publication to pause affected trades>,
  "reasoning": "<one sentence explaining your classification>"
}

Guidance:
- "high" impact (embargo 60-180 min): central bank rate decisions, CPI/PCE prints, NFP, GDP, war/invasion headlines, major OPEC decisions, surprise rate moves, market-crash headlines.
- "medium" impact (embargo 30-60 min): inflation/unemployment commentary, minor central bank speeches, PMI, retail sales, sanctions news, commodity supply concerns.
- "low" impact (embargo 10-30 min): individual earnings reports, sector news, secondary data.
- "none" (embargo 0): articles that are reporting OLD news, opinion pieces, feature stories, lifestyle/sports, sector-neutral corporate news, or articles where the keyword match is coincidental (e.g. "Warwickshire" for "war").

Only include buckets that are actually affected. A US CPI print affects USD_MAJOR, METALS, EQUITY_US, JPY_CROSS. An ECB decision affects USD_MAJOR, EQUITY_EU, JPY_CROSS. Oil-supply news affects ENERGY and often EQUITY_US.

If the article is clearly irrelevant to market trading (lifestyle, sports, obituaries, coincidental keyword matches), return impact: "none" with empty buckets and embargo_minutes: 0.

Output ONLY the JSON. No markdown, no prose before or after."""


# ── newsdata.io fetch ─────────────────────────────────────────────────────────

async def _fetch_latest(session: aiohttp.ClientSession, apikey: str, q: str,
                        language: str = "en", size: int = 10) -> List[Dict[str, Any]]:
    params = {"apikey": apikey, "q": q, "language": language, "size": str(size)}
    try:
        async with session.get(NEWSDATA_BASE, params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
            if data.get("status") != "success":
                logger.warning("newsdata.io non-success: %s", data.get("message") or data)
                return []
            return data.get("results") or []
    except Exception:
        logger.exception("newsdata.io fetch failed (q=%r)", q)
        return []


# ── Claude classifier ─────────────────────────────────────────────────────────

def _classify_article(client, title: str, description: str) -> Optional[Dict[str, Any]]:
    """Ask the LLM to classify one article. Returns None on error."""
    user_msg = (
        f"TITLE: {(title or '').strip()[:400]}\n\n"
        f"DESCRIPTION: {(description or '').strip()[:800]}"
    )
    try:
        resp = client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            max_tokens=512,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        # Strip markdown fences if the model wrapped its output
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rsplit("```", 1)[0].strip()
        obj = json.loads(text)
        # Validate shape
        impact = obj.get("impact", "none")
        if impact not in ("high", "medium", "low", "none"):
            impact = "none"
        buckets = [b for b in obj.get("affected_buckets") or [] if b in ALLOWED_BUCKETS]
        symbols = [s for s in obj.get("affected_symbols") or [] if isinstance(s, str)]
        em = int(obj.get("embargo_minutes") or 0)
        return {
            "impact": impact,
            "event_type": str(obj.get("event_type") or "unclassified")[:40],
            "affected_buckets": buckets,
            "affected_symbols": symbols[:20],
            "embargo_minutes": max(0, min(em, 480)),  # clamp to [0, 8h]
            "reasoning": str(obj.get("reasoning") or "")[:300],
        }
    except Exception:
        logger.exception("classifier error")
        return None


# ── Persistence ───────────────────────────────────────────────────────────────

def _parse_pub_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


async def _already_cached(pool: asyncpg.Pool, article_id: str) -> bool:
    row = await pool.fetchrow(
        "SELECT 1 FROM ml_news_events WHERE article_id = $1", article_id
    )
    return row is not None


async def _persist(pool: asyncpg.Pool, article: Dict[str, Any],
                   classification: Optional[Dict[str, Any]]) -> bool:
    article_id = article.get("article_id")
    if not article_id:
        return False
    pub = _parse_pub_date(article.get("pubDate"))
    now = datetime.now(timezone.utc)
    embargo_until = None
    event_type = impact = None
    buckets: List[str] = []
    symbols: List[str] = []
    if classification and classification["impact"] != "none" and classification["embargo_minutes"] > 0:
        impact = classification["impact"]
        event_type = classification["event_type"]
        buckets = classification["affected_buckets"]
        symbols = classification["affected_symbols"]
        base = pub or now
        cand = base + timedelta(minutes=classification["embargo_minutes"])
        if cand > now:
            embargo_until = cand
    elif classification:
        # Record "none" classification for auditability — impact column stores it.
        impact = "none"
        event_type = classification["event_type"]

    row = await pool.fetchrow(
        """
        INSERT INTO ml_news_events (
            article_id, title, description, link, source, published_at,
            category, country,
            event_type, impact, affected_buckets, affected_symbols,
            embargo_until
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
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
        event_type, impact, buckets, symbols,
        embargo_until,
    )
    return row is not None


# ── Oracle hot-path helper (DB-only, no external API) ─────────────────────────

async def active_embargoes_for(pool: asyncpg.Pool, symbol: str,
                               bucket: Optional[str]) -> List[Dict[str, Any]]:
    """Return currently-active embargoes affecting this symbol or bucket."""
    rows = await pool.fetch(
        """
        SELECT id, event_type, impact, embargo_until, title,
               affected_buckets, affected_symbols
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


# ── Refresh-cache entrypoint (cron calls this) ────────────────────────────────

async def refresh_cache(dry_run: bool = False) -> Dict[str, int]:
    cfg = get_config()
    apikey = os.environ.get("NEWSDATA_API_KEY", "").strip()
    if not apikey:
        raise RuntimeError("NEWSDATA_API_KEY missing from .env")

    # OpenAI client (picks up OPENAI_API_KEY from env)
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai SDK not installed") from e
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY missing from .env")
    client = OpenAI()

    totals = {"fetched": 0, "new_articles": 0, "classified": 0,
              "embargoed": 0, "skipped_existing": 0, "classify_errors": 0}
    pool = await asyncpg.create_pool(cfg.db_dsn, min_size=1, max_size=4)
    try:
        async with aiohttp.ClientSession() as session:
            # Fetch all queries, deduplicate by article_id across the batch
            all_articles: List[Dict[str, Any]] = []
            seen_ids = set()
            for q in NEWSDATA_QUERIES:
                arts = await _fetch_latest(session, apikey, q, size=10)
                for a in arts:
                    aid = a.get("article_id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        all_articles.append(a)
                logger.info("fetched %d articles for q=%r", len(arts), q)
            totals["fetched"] = len(all_articles)

            # Filter to unseen articles only (skip re-classifying cached ones)
            new_articles = []
            for a in all_articles:
                if await _already_cached(pool, a["article_id"]):
                    totals["skipped_existing"] += 1
                else:
                    new_articles.append(a)
            totals["new_articles"] = len(new_articles)

            # Cap classification calls
            if len(new_articles) > MAX_CLASSIFY_PER_RUN:
                logger.warning("capping classify batch: %d new -> %d",
                               len(new_articles), MAX_CLASSIFY_PER_RUN)
                new_articles = new_articles[:MAX_CLASSIFY_PER_RUN]

            # Classify + persist
            for a in new_articles:
                classification = await asyncio.to_thread(
                    _classify_article, client,
                    a.get("title") or "", a.get("description") or "",
                )
                if classification is None:
                    totals["classify_errors"] += 1
                    continue
                totals["classified"] += 1
                if classification["impact"] in ("high", "medium") and classification["embargo_minutes"] > 0:
                    totals["embargoed"] += 1
                    logger.warning(
                        "NEWS CLASSIFIED impact=%s type=%s buckets=%s embargo=%dm title=%r",
                        classification["impact"], classification["event_type"],
                        classification["affected_buckets"], classification["embargo_minutes"],
                        (a.get("title") or "")[:100],
                    )

                if not dry_run:
                    await _persist(pool, a, classification)

        logger.info("refresh_cache done: %s", totals)
        return totals
    finally:
        await pool.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh news-embargo cache via newsdata.io + Claude classifier.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch + classify without persisting.")
    args = ap.parse_args()

    cfg = get_config()
    configure_logging(cfg)
    totals = await refresh_cache(dry_run=args.dry_run)
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
