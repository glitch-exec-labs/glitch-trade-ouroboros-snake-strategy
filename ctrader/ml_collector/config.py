"""
Config loader for the ML collector v2.

Loads the collector's own .env (NEVER the production platform's .env).
Parses ML_BOTS JSON (6 bots with per-bot model + timeframe + account),
ML_SYMBOLS list, and ML_DATABASE_URL for PostgreSQL.

Hardcodes live=False for cTrader and asserts no account matches
ML_FORBIDDEN_ACCOUNT_ID (set in .env to the production live account).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

_COLLECTOR_DIR = Path(__file__).resolve().parent
_ENV_PATH = _COLLECTOR_DIR / ".env"


def _forbidden_live_account_id() -> int:
    """Resolved at runtime (after .env is loaded) so the real value stays
    out of the repo. Set ML_FORBIDDEN_ACCOUNT_ID in the runtime .env."""
    return int(os.environ.get("ML_FORBIDDEN_ACCOUNT_ID", "0") or "0")

logger = logging.getLogger("ml_collector.config")


@dataclass(frozen=True)
class BotConfig:
    name: str                  # "viper", "cobra", "mamba", "hydra", "taipan", "anaconda"
    model: str                 # "momentum_hunter", "mamba_reversion", etc.
    timeframe: str             # "m1", "m5", "m15", "m30", "h1", "h4"
    tf_enum: int               # cTrader period enum: 1, 5, 7, 8, 9, 10
    account_id: int            # ctidTraderAccountId (demo only)
    lots: float                # fallback lot size if notional_pct is 0
    notional_pct: float        # adaptive sizing: target notional = balance × notional_pct
    min_confidence: float      # signal threshold
    max_concurrent: int        # max open trades per symbol
    bar_count: int             # how many bars to fetch


@dataclass(frozen=True)
class Config:
    # cTrader app credentials (shared across all demo accounts)
    ctrader_client_id: str
    ctrader_client_secret: str
    ctrader_access_token: str
    price_feed_account_id: int
    # PostgreSQL
    db_dsn: str
    # Symbols to collect data for
    symbols: List[str]
    # 6 bot configurations
    bots: List[BotConfig]
    # Paths
    state_dir: Path
    # Cadence
    loop_interval_seconds: int
    position_poll_interval_seconds: int
    # Observability
    log_level: str


_cached: Config | None = None


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var {name}. Check {_ENV_PATH}")
    return val


def _parse_bots(raw: str) -> List[BotConfig]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ML_BOTS is not valid JSON: {e}") from e
    if not isinstance(items, list) or not items:
        raise RuntimeError("ML_BOTS must be a non-empty JSON array")
    out: List[BotConfig] = []
    for i, it in enumerate(items):
        try:
            bot = BotConfig(
                name=str(it["name"]),
                model=str(it["model"]),
                timeframe=str(it["timeframe"]),
                tf_enum=int(it["tf_enum"]),
                account_id=int(it["account_id"]),
                lots=float(it["lots"]),
                notional_pct=float(it.get("notional_pct", 1.0)),
                min_confidence=float(it["min_confidence"]),
                max_concurrent=int(it.get("max_concurrent", 1)),
                bar_count=int(it.get("bar_count", 200)),
            )
            forbidden = _forbidden_live_account_id()
            if forbidden and bot.account_id == forbidden:
                raise RuntimeError(
                    f"Bot {bot.name!r} uses a forbidden account_id. Refusing to start."
                )
            out.append(bot)
        except (KeyError, ValueError, TypeError) as e:
            raise RuntimeError(f"ML_BOTS[{i}] is malformed: {e}") from e
    return out


def _parse_symbols(raw: str) -> List[str]:
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        raise RuntimeError("ML_SYMBOLS must be a non-empty comma-separated list")
    return symbols


def _proxy_env_to_ctrader_namespace(cfg: Config) -> None:
    """
    CTraderPriceFeed reads os.environ["CTRADER_*"] directly in __init__.
    We proxy our ML_* values and force CTRADER_LIVE=false.
    """
    os.environ["CTRADER_CLIENT_ID"] = cfg.ctrader_client_id
    os.environ["CTRADER_CLIENT_SECRET"] = cfg.ctrader_client_secret
    os.environ["CTRADER_ACCESS_TOKEN"] = cfg.ctrader_access_token
    os.environ["CTRADER_ACCOUNT_ID"] = str(cfg.price_feed_account_id)
    os.environ["CTRADER_LIVE"] = "false"


def get_config() -> Config:
    global _cached
    if _cached is not None:
        return _cached

    if not _ENV_PATH.exists():
        raise RuntimeError(f"ML collector .env not found at {_ENV_PATH}")
    load_dotenv(_ENV_PATH, override=True)

    bots = _parse_bots(_require("ML_BOTS"))
    symbols = _parse_symbols(_require("ML_SYMBOLS"))

    price_feed_account = int(
        os.environ.get("ML_PRICE_FEED_ACCOUNT_ID") or bots[0].account_id
    )
    forbidden = _forbidden_live_account_id()
    if forbidden and price_feed_account == forbidden:
        raise RuntimeError("price_feed_account_id must not be the production live account")

    cfg = Config(
        ctrader_client_id=_require("ML_CTRADER_CLIENT_ID"),
        ctrader_client_secret=_require("ML_CTRADER_CLIENT_SECRET"),
        ctrader_access_token=_require("ML_CTRADER_ACCESS_TOKEN"),
        price_feed_account_id=price_feed_account,
        db_dsn=_require("ML_DATABASE_URL"),
        symbols=symbols,
        bots=bots,
        state_dir=Path(os.environ.get("ML_STATE_DIR", str(_COLLECTOR_DIR / "state"))),
        loop_interval_seconds=int(os.environ.get("ML_LOOP_INTERVAL_SECONDS", "60")),
        position_poll_interval_seconds=int(os.environ.get("ML_POSITION_POLL_SECONDS", "30")),
        log_level=os.environ.get("ML_LOG_LEVEL", "INFO").upper(),
    )

    _proxy_env_to_ctrader_namespace(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    _cached = cfg
    logger.info(
        "Config loaded: db=%s symbols=%d bots=%s",
        cfg.db_dsn.split("@")[-1],
        len(cfg.symbols),
        [(b.name, b.model, b.timeframe, b.account_id) for b in cfg.bots],
    )
    return cfg


def configure_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
