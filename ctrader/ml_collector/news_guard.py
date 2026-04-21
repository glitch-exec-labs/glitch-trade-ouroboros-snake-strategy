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
            embargo_until, embargo_from
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NULL)
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
          AND (embargo_from IS NULL OR embargo_from <= NOW())
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


# ── Economic calendar (ForexFactory) ────────────────────────────────────
#
# Scheduled macro events are fetched from ForexFactory's free weekly JSON feed
# and inserted into ml_news_events with embargo_from/embargo_until windows
# tuned per event type. Unlike reactive news these are deterministic —
# structured title + country + impact — so no LLM call is needed.

FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Country (ForexFactory uses 3-letter currency) → correlation buckets that
# care about macro news from that country. Must match oracle.CORRELATION_BUCKETS.
COUNTRY_BUCKETS: Dict[str, List[str]] = {
    "USD": ["USD_MAJOR", "METALS", "EQUITY_US", "JPY_CROSS"],
    "EUR": ["USD_MAJOR", "EQUITY_EU"],
    "GBP": ["USD_MAJOR", "EQUITY_EU", "JPY_CROSS"],
    "JPY": ["USD_MAJOR", "JPY_CROSS", "EQUITY_AS"],
    "AUD": ["USD_MAJOR"],
    "NZD": ["USD_MAJOR"],
    "CAD": ["USD_MAJOR", "ENERGY"],   # CAD is oil-linked
    "CHF": ["USD_MAJOR"],
    "CNY": ["METALS", "EQUITY_AS", "EQUITY_US"],
}

# Event-type keyword matching → (event_type, window_before_min, window_after_min, impact_override)
# Matched in order; first hit wins. All keywords are lowercased before comparison.
_CAL_EVENT_RULES: List[Tuple[str, str, int, int, Optional[str]]] = [
    # keyword,              event_type,          before, after, impact_override
    ("fomc",                "fomc",              60,     180,   "high"),
    ("fed chair",           "fed_chair",         45,     120,   "high"),
    ("fed funds rate",      "fed_rate",          60,     180,   "high"),
    ("rate decision",       "rate_decision",     60,     120,   "high"),
    ("interest rate",       "interest_rate",     60,     120,   "high"),
    ("bank rate",           "rate_decision",     45,     90,    "high"),
    ("monetary policy",     "monetary_policy",   45,     90,    "high"),
    ("press conference",    "cb_press",          30,     60,    "high"),
    ("non-farm",            "nfp",               45,     120,   "high"),
    ("nonfarm",             "nfp",               45,     120,   "high"),
    ("cpi",                 "cpi",               45,     90,    "high"),
    ("core pce",            "pce",               45,     90,    "high"),
    ("pce price",           "pce",               45,     90,    "high"),
    ("gdp",                 "gdp",               30,     60,    "high"),
    ("retail sales",        "retail_sales",      30,     60,    "medium"),
    ("unemployment",        "unemployment",      30,     60,    "medium"),
    ("claimant count",      "claimant_count",    30,     45,    "medium"),
    ("jobless",             "jobless_claims",    20,     30,    "medium"),
    ("pmi",                 "pmi",               20,     45,    "medium"),
    ("ifo",                 "ifo",               20,     30,    "medium"),
    ("zew",                 "zew",               20,     30,    "medium"),
    ("ppi",                 "ppi",               20,     30,    "medium"),
    ("trade balance",       "trade_balance",     15,     30,    None),   # impact from calendar
    ("consumer confidence", "consumer_conf",     15,     30,    None),
    ("ism",                 "ism",               20,     45,    "medium"),
    ("crude oil inventor",  "oil_inventory",     15,     30,    None),
    ("natural gas",         "nat_gas",           15,     30,    None),
    ("testif",              "central_bank_testify", 30,  60,    None),
    ("speaks",              "central_bank_speech",  20,  45,    None),
]


def _ff_event_classify(title: str, country: str, impact_ff: str
                       ) -> Optional[Dict[str, Any]]:
    """Map a ForexFactory calendar event to our schema.
    Returns None for events we do not want to embargo (Low impact, no match)."""
    t = (title or "").lower()
    # Pick the first matching rule
    matched = None
    for kw, ev_type, before, after, impact_override in _CAL_EVENT_RULES:
        if kw in t:
            matched = (ev_type, before, after, impact_override)
            break

    # For unmatched HIGH events from a major currency, still embargo with default window
    if matched is None:
        if impact_ff == "High":
            matched = ("scheduled_high", 30, 60, "high")
        else:
            return None

    ev_type, before, after, impact_override = matched
    impact = impact_override or impact_ff.lower()
    if impact == "low":
        return None

    buckets = COUNTRY_BUCKETS.get(country.upper(), [])
    if not buckets:
        return None  # currencies we don't care about (e.g., SEK, ZAR)
    return {
        "event_type": ev_type,
        "impact": impact,
        "affected_buckets": buckets,
        "affected_symbols": [],
        "window_before_min": before,
        "window_after_min": after,
    }


def _parse_ff_date(s: str) -> Optional[datetime]:
    # ForexFactory format: "2026-04-21T08:30:00-04:00"  (NY time with offset)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def _fetch_ff_calendar(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    try:
        async with session.get(FF_WEEK_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning("ForexFactory HTTP %d", r.status)
                return []
            return await r.json(content_type=None)
    except Exception:
        logger.exception("ForexFactory fetch failed")
        return []


async def refresh_econ_calendar(pool: asyncpg.Pool, dry_run: bool = False
                                ) -> Dict[str, int]:
    """Populate ml_news_events with upcoming scheduled macro events."""
    totals = {"fetched": 0, "inserted": 0, "skipped_past": 0,
              "skipped_low_irrelevant": 0, "skipped_existing": 0}
    async with aiohttp.ClientSession() as session:
        events = await _fetch_ff_calendar(session)
    totals["fetched"] = len(events)
    now = datetime.now(timezone.utc)

    for ev in events:
        when = _parse_ff_date(ev.get("date") or "")
        if when is None:
            continue
        when = when.astimezone(timezone.utc)

        # Classify
        cls = _ff_event_classify(ev.get("title") or "", ev.get("country") or "",
                                  ev.get("impact") or "Low")
        if cls is None:
            totals["skipped_low_irrelevant"] += 1
            continue

        embargo_from = when - timedelta(minutes=cls["window_before_min"])
        embargo_until = when + timedelta(minutes=cls["window_after_min"])

        # Skip events whose embargo window is already over
        if embargo_until <= now:
            totals["skipped_past"] += 1
            continue

        # Stable article_id so re-runs are idempotent
        import hashlib
        key = f"ffcal:{when.strftime('%Y%m%dT%H%M')}:{ev.get('country')}:{(ev.get('title') or '').strip()}"
        art_id = "ffcal:" + hashlib.sha1(key.encode()).hexdigest()[:24]

        if dry_run:
            logger.info("DRY %s %s %s %s embargo=[%s, %s]",
                        when.strftime("%Y-%m-%d %H:%M UTC"), ev.get("country"),
                        ev.get("impact"), (ev.get("title") or "")[:60],
                        embargo_from.strftime("%H:%M"), embargo_until.strftime("%H:%M"))
            continue

        row = await pool.fetchrow(
            """
            INSERT INTO ml_news_events
                (article_id, title, description, source, published_at,
                 event_type, impact, affected_buckets, affected_symbols,
                 embargo_from, embargo_until)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (article_id) DO NOTHING
            RETURNING id
            """,
            art_id,
            (ev.get("title") or "")[:400],
            f"ForexFactory {ev.get('country')} {ev.get('impact')}: "
            f"forecast={ev.get('forecast') or '-'}, previous={ev.get('previous') or '-'}",
            "forexfactory",
            when,
            cls["event_type"], cls["impact"],
            cls["affected_buckets"], cls["affected_symbols"],
            embargo_from, embargo_until,
        )
        if row is not None:
            totals["inserted"] += 1
            logger.info("CAL scheduled %s %s %s impact=%s embargo=[%s → %s]",
                        when.strftime("%m-%d %H:%M UTC"), ev.get("country"),
                        cls["event_type"], cls["impact"],
                        embargo_from.strftime("%H:%M"), embargo_until.strftime("%H:%M UTC"))
        else:
            totals["skipped_existing"] += 1
    return totals


async def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh embargo cache: ForexFactory calendar + newsdata.io reactive news.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch + classify without persisting.")
    ap.add_argument("--skip-news", action="store_true", help="Only refresh the economic calendar, skip LLM news.")
    ap.add_argument("--skip-calendar", action="store_true", help="Only refresh LLM news, skip the calendar.")
    args = ap.parse_args()

    cfg = get_config()
    configure_logging(cfg)

    combined = {}
    if not args.skip_calendar:
        pool = await asyncpg.create_pool(cfg.db_dsn, min_size=1, max_size=2)
        try:
            cal_totals = await refresh_econ_calendar(pool, dry_run=args.dry_run)
            combined["calendar"] = cal_totals
        finally:
            await pool.close()
    if not args.skip_news:
        news_totals = await refresh_cache(dry_run=args.dry_run)
        combined["news"] = news_totals

    print(json.dumps(combined, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
