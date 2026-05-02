# Ouroboros Project Memory

Operational context for Claude sessions working on this repo. Read this first.

## What This Is

Public GitHub repo: `glitch-exec-labs/glitch-ouroboros-snake-strategy` (now redirected to `glitch-trade-ouroboros-snake-strategy` — update remote URL at some point).

Flagship 6-bot cTrader ensemble with Oracle coordination, portfolio risk gate, and a PostgreSQL-backed ML data collection loop. The six bots (hydra M1, viper M5, mamba M15, taipan M30, cobra H1, anaconda H4) run in parallel on dedicated cTrader demo accounts. Goal: accumulate ~6 months of labelled training data for eventual model retraining.

## Deployment Layout

- Repo lives at `/opt/glitch-ouroboros` — owned by `glitchml:glitchml`
- Runtime `.env`: `/opt/glitch-ouroboros/ctrader/ml_collector/.env` (gitignored)
- Python venv: `/opt/glitch-ouroboros/ctrader/ml_collector/venv`
- systemd services:
  - `glitch-ml-collector.service` — persistent collector (6 bot loops + monitor loop + orphan watchdog)
  - `glitch-ml-export.service` + `.timer` — daily CSV export at 00:10 UTC
- PostgreSQL DB: `glitch_ml` on localhost, user `glitchml`, creds in the `.env`

## Permissions Quirks — Don't Forget

Repo files are owned by `glitchml` but this session runs as `support`. The `Write` / `Edit` tools FAIL with EACCES on most files in the repo. Workarounds:

- **Write/overwrite files**: `sudo tee /path/to/file > /dev/null << 'EOF' ... EOF`
- **Surgical edits**: `sudo python3 << 'PYEOF' ... PYEOF` (read file, modify in-memory, write back)
- **Git operations**: `sudo -u glitchml git -c user.name="glitchml" -c user.email="glitchml@glitch" <cmd>`
- **Push**: `git -C /opt/glitch-ouroboros push` as the support user (has the GitHub SSH credentials). Ignore the `cannot lock ref 'refs/remotes/origin/main'` stderr warning — the actual push succeeds.

Always push after committing — cron git-sync at 15-min cadence is unreliable for this repo (upstream is set, but don't trust it).

## Core Architecture

```
bot_loop × 6  →  ml_signals  →  confidence gate  →  sizer  →  Oracle risk gate  →  order_placer  →  ml_trades
                                                                                         │
                                                                                         ▼
                                                                                   monitor_loop
                                                                                   (closure detect
                                                                                    + orphan backfill)
```

Oracle has two responsibilities:

1. **Pre-trade risk gate** (`oracle.check_trade_allowed`) — called before EVERY order. Blocks trades that would exceed portfolio-wide caps (symbol, correlation bucket, global). Hard discipline layer. Limits live in `ml_oracle_risk_limits` table, tunable via UPDATE without restart.

2. **Shadow ensemble voting** (`oracle.oracle_loop`) — reads latest signal per bot per symbol, weights by `ml_oracle_weights`, writes to `ml_oracle_decisions`. Shadow mode only (no execution). Meant to be calibrated against real bot outcomes for 2–4 weeks before wiring live.

Correlation buckets in `oracle.CORRELATION_BUCKETS`: USD_MAJOR, JPY_CROSS, METALS, ENERGY, EQUITY_US, EQUITY_EU, EQUITY_AS.

## Public Engine / Private Tuning Split

**Phase 1 done.** Everything that represents real tuning is loaded from disk, not hardcoded:

- `.env` → credentials, `ML_BOTS` JSON (per-bot `min_confidence`, `notional_pct`, `account_id`, symbols)
- `ctrader/ensemble/models/<name>.params.json` → per-model tuning (gitignored)
- `ctrader/ensemble/models/<name>.params.example.json` → demo values (in git, intentionally neutral)
- DB tables `ml_oracle_weights` + `ml_oracle_risk_limits` → Oracle calibration, runtime-tunable via SQL
- 93 tuneable numeric params across 7 model files, externalized via `BaseModel.p("key", default)` in `base_model.py`

**Phase 2 (not done yet):** separate private companion repo for the ML training pipeline, backtest engine, versioned production `.params.json`. Build this when you actually have the training pipeline.

## Key Files

```
ctrader/
├── executor/
│   ├── ctrader_client.py      # Async TCP/SSL cTrader client. _build_frame(msg_obj, mid) — NOT (int, bytes, mid)
│   └── protobuf.py            # Vendored Protobuf helper from spotware/OpenApiPy
├── ml_collector/
│   ├── collector.py           # Main async loop: 6 bot_loops + monitor_loop + orphan watchdog
│   ├── strategy_runner.py     # Model evaluation; contains _MODEL_CANDLE_KEY (CRITICAL — see gotchas)
│   ├── oracle.py              # Pre-trade gate + shadow voting
│   ├── order_placer.py        # place_market_order (uses symbol spec cache)
│   ├── sizer.py               # Adaptive lot sizing (balance × notional_pct × streak_mult)
│   ├── reconcile_stale.py     # One-shot: close DB-open trades that are closed at broker
│   ├── close_orphans.py       # One-shot: close broker-open positions not in ml_trades
│   ├── db.py                  # All PostgreSQL I/O
│   ├── schema.sql             # Initial schema
│   └── migrations/
│       ├── 0002_oracle.sql
│       └── 0003_oracle_risk_gate.sql
└── ensemble/models/           # 7 signal models, each with .py + .params.example.json
```

## Recent Gotchas Worth Remembering

1. **`_build_frame` signature changed** in the Protobuf helper refactor. Old: `_build_frame(payload_type_int, payload_bytes, mid)`. New: `_build_frame(msg_obj, mid)`. Any new call site must use the new signature. The refactor initially left `order_placer.py` and `sizer.py` on the old signature → silent trade failure for hours until 373 TypeErrors showed up in logs.

2. **`_MODEL_CANDLE_KEY` in `strategy_runner.py`** routes each model to `candles["m15"]` or `candles["h1"]`. Models that expect `h1` (trend_follower, volume_profiler, session_analyst) silently return HOLD if given only `m15`. Add new models here FIRST.

3. **`close_trade` requires `signal_id`** — the FK is NOT NULL. For orphan backfills, create a synthetic `ml_signals` row first (reasoning='broker_orphan_backfill', bar_time=None to avoid dedup), then insert the trade referencing it.

4. **cTrader demo deal history expires** in ~days. `get_deals_by_position` calls on old tickets return empty. Fallback path in monitor_loop uses bar-price estimation; reconcile_stale uses `outcome='UNKNOWN'` + `exit_reason='broker_closed_stale'`.

5. **Symbol not found** errors for XTIUSD / US100 are expected — broker doesn't carry those on demo. Ignore in log noise filters.

## Security Posture

- `.env` never committed (gitignored, always was)
- `.params.json` never committed (gitignored after Phase 1)
- One dead Brave Search API key sits in pre-`69623cd` history (`mt5/bots/news_guard.py`). Account is **suspended** — key is inert. We deliberately did NOT force-push to scrub it because cost (breaks every clone/fork of a public repo) exceeds benefit (obscuring a dead credential). See `SECURITY_NOTE.md`.

**Rule**: `.gitignore` only prevents NEW commits. Anything already in history requires `git filter-repo` + force-push to remove — and rotation of the underlying secret first. Never force-push without rotating; rotating usually means force-push is unnecessary.

## Key DB Tables

| Table | Purpose |
|---|---|
| `ml_bars` | OHLCV archive, unique per (symbol, tf, bar_time) |
| `ml_signals` | Every model evaluation (including HOLDs). Dedup on (bot, symbol, bar_time) |
| `ml_trades` | Every order opened + outcome. `closed_at IS NULL` = live |
| `ml_oracle_weights` | Per-bot ensemble weights + veto flag |
| `ml_oracle_decisions` | Shadow-mode ensemble votes (one per symbol per Oracle cycle) |
| `ml_oracle_risk_limits` | Hard caps: `('symbol','EURUSD')` / `('bucket','USD_MAJOR')` / `('global','ALL')` |
| `ml_oracle_blocks` | Every blocked trade attempt (still labelled data) |
| `ml_collector_state` | Runtime state KV store |

DB connect: `PGPASSWORD=... psql -h localhost -U glitchml -d glitch_ml` (creds in `.env`).

## Restart / Operational Commands

```bash
# restart collector
sudo systemctl restart glitch-ml-collector.service

# tail logs
sudo journalctl -u glitch-ml-collector.service -f

# one-shot utilities
cd /opt/glitch-ouroboros/ctrader
sudo -u glitchml ml_collector/venv/bin/python -m ml_collector.reconcile_stale --dry-run
sudo -u glitchml ml_collector/venv/bin/python -m ml_collector.close_orphans --dry-run
sudo -u glitchml ml_collector/venv/bin/python -m ml_collector.oracle   # runs Oracle shadow loop standalone

# adjust Oracle limits without restart
PGPASSWORD=... psql ... -c "UPDATE ml_oracle_risk_limits SET max_lots=3.0 WHERE scope_type='symbol' AND scope_key='EURUSD';"

# tune a model without code change
cd /opt/glitch-ouroboros/ctrader/ensemble/models
sudo -u glitchml cp momentum_hunter.params.example.json momentum_hunter.params.json
sudo -u glitchml vim momentum_hunter.params.json
sudo systemctl restart glitch-ml-collector.service
```

## Open Follow-Ups

- **Per-bot symbol whitelists** — DONE 2026-05-02 (commit `6e3f2c1` plumbing,
  `.env` updated with empirical assignments below). BotConfig now supports
  optional `symbols`, `min_confidence_per_symbol`, and `notional_pct_per_symbol`
  fields. Empty / missing → falls back to global `cfg.symbols` /
  `bot.min_confidence` / `bot.notional_pct` (backward compatible).

  Current per-bot symbol assignments (pulled from production `.env`,
  derived from outcome stats over the first 1,950 closed trades):
  - hydra (M1):    15 symbols — broad: forex majors + crypto + 4 indices + metals (cut: US500, GBPUSD)
  - viper (M5):    5 symbols  — concentrated: BTCUSD, UK100, XAUUSD, XAGUSD, NZDUSD (cut: JPN225 -$346k disaster, all losing forex pairs)
  - mamba (M15):   3 symbols  — narrow: BTCUSD, JPN225, USDCHF (small dataset; will widen as data grows)
  - taipan (M30):  7 symbols  — indices + crypto + metals: BTCUSD, GER40, UK100, XAUUSD, XAGUSD, USDJPY, NZDUSD (cut: JPN225 -$26k, US500 -$8k)
  - cobra (H1):    6 symbols  — slow trending: JPN225, GER40, US500, XAGUSD, USDJPY, USDCAD (cut: XAUUSD 14% WR)
  - anaconda (H4): 6 symbols  — architectural (insufficient outcome data): EURUSD, GBPUSD, USDJPY, XAUUSD, GER40, UK100

  Per-cycle evaluation footprint dropped from 120 (bot, symbol) pairs to 42.

  Related polish still pending (the per-symbol-override fields exist in
  config but are unused so far):
  - Per-symbol `min_confidence` overrides (e.g. require 0.85 on noisy crypto,
    0.65 on smooth majors). Tune after ~2 more weeks of data.
  - Per-symbol `notional_pct` overrides (e.g. size BTC trades smaller than
    forex). Currently all symbols use the bot-wide notional_pct.
  - Per-bot news-embargo whitelist (crypto bots ignore CPI/FOMC, still
    respect geopolitical events) — schema not yet built.

- **Crypto sizer caps** (priority: low, evaluate after 1 week of crypto data).
  The adaptive sizer naturally proposes 256+ SOLUSD lots and 9.5 ETHUSD lots
  to hit a $25k notional target because per-lot underlying value is small.
  Currently only BTCUSD trades through the gate; ETHUSD/SOLUSD always block on
  `symbol_lots_cap`. After 1 week of BTCUSD-only data, decide whether to
  loosen caps (UPDATE ml_oracle_risk_limits SOLUSD → 400, ETHUSD → 30) or add
  a per-symbol `max_lots_per_trade` clamp in sizer.py.

- **Monitor Oracle shadow output** for 2–4 weeks against bot outcomes before wiring live ensemble execution.
- **Phase 2 (private companion repo)** when building the ML training pipeline on the 6-month dataset.
- Remote URL rename: `git remote set-url origin git@github.com:glitch-exec-labs/glitch-trade-ouroboros-snake-strategy.git` when convenient.
- `mean_reverter.py` has `rsi_extreme` divisor wired to `rsi_oversold` param (same default, identical behaviour). Split to `rsi_extreme_divisor` if isolation matters.
- `session_analyst.py` has `ema_slope_window * 2` as the min-bars guard. Split if isolation matters.
