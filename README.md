<div align="center">

# Glitch Trade Ouroboros Snake Strategy

Glitch Trade's flagship multi-bot ensemble, combining Oracle coordination with the six-snake execution stack.

![Product](https://img.shields.io/badge/product-Flagship%20Ensemble-0f766e?style=for-the-badge)
![Family](https://img.shields.io/badge/family-glitch%20trading-111827?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-MT5%20today%20%7C%20cTrader%20next-111827?style=for-the-badge)
![Scope](https://img.shields.io/badge/scope-oracle%20%7C%20ensemble%20%7C%20risk-1d4ed8?style=for-the-badge)

[Glitch Trading Core](https://github.com/glitch-exec-labs/glitch-trading-core) · [Indian King Cobra](https://github.com/glitch-exec-labs/glitch-indian-king-cobra) · [Terciopelo](https://github.com/glitch-exec-labs/glitch-terciopelo)

</div>

> Part of **Glitch Trade**, the trading domain inside **Glitch Executor Labs** — one builder shipping products across **Trade**, **Edge**, and **Grow**.

> Ouroboros Snake Strategy is the flagship coordinated Glitch ensemble: six specialized execution bots, one Oracle layer, and a portfolio-aware risk model designed to keep the stack coherent across any market regime.

---

## Glitch Trading Family

```mermaid
flowchart LR
    A[Glitch Trading Core] --> B[Ouroboros Snake Strategy]
    A --> C[Indian King Cobra]
    A --> D[Terciopelo]
    B --> E[Flagship Multi-Bot Ensemble]
```

---

## How The Engine Works

At its core Ouroboros is a **signal collection and execution loop**. Six bots run in parallel, each watching the same symbol universe through a different lens. A confidence gate decides which signals become orders. An Oracle layer arbitrates conflicts. A monitor loop watches what happens after execution and feeds outcomes back into the record.

```mermaid
flowchart TD
    A["Market Data (cTrader API)"] --> B["Bar Fetcher\n(per bot, per symbol)"]
    B --> C["Strategy Runner\n(model evaluation)"]
    C --> D["ml_signals\n(every bar, every symbol)"]
    D --> E{"Confidence Gate\nconfidence ≥ min_confidence\nno open trade for symbol"}
    E -->|pass| F["Sizer\n(adaptive lot calculation)"]
    F --> G["Order Placer\n(cTrader NewOrderReq)"]
    G --> H["ml_trades\n(entry recorded)"]
    H --> I["Monitor Loop\n(polls every 30 s)"]
    I --> J{"Position closed\nin cTrader?"}
    J -->|yes| K["Deal History API\n(authoritative exit price + PnL)"]
    K --> L["ml_trades updated\n(outcome, pnl, exit_price)"]
    J -->|no| I
    D --> M["ml_bars\n(OHLCV archive)"]
```

---

## Bot Matrix

| Bot | Timeframe | Model | Role |
|---|---|---|---|
| `hydra` | M1 | adaptive tactical | regime routing, fastest reaction |
| `viper` | M5 | momentum hunter | fast directional momentum + pullback |
| `mamba` | M15 | mean reverter | range balance and reversion |
| `taipan` | M30 | session analyst | session breakout and expansion capture |
| `cobra` | H1 | trend follower | structure + price action, higher conviction |
| `anaconda` | H4 | volume profiler | slower structural continuation |

Each bot runs its own cTrader demo account. Signals from all six accumulate in the same PostgreSQL database, giving six independent views of the same market.

---

## Signal Models

| Model | Candle Key | Signal Basis |
|---|---|---|
| `momentum_hunter` | m15 | momentum burst + pullback alignment |
| `mamba_reversion` | m15 | mean reversion from range extremes |
| `mean_reverter` | m15 | statistical mean reversion |
| `trend_follower` | h1 | higher-timeframe structure and trend continuation |
| `volume_profiler` | h1 | volume-weighted price levels and imbalance |
| `session_analyst` | h1 | session boundary expansion and key level breaks |
| `multi_tf_align` | m15 | multi-timeframe confluence alignment |

The candle key is how the strategy runner knows which timeframe bar set to hand a given model. Getting this wrong causes the model to always return HOLD — the most common silent failure mode.

---

## Configuration: `ML_BOTS`

The entire ensemble is driven by a single JSON array in the runtime `.env`:

```
ML_BOTS='[...]'
```

Each element configures one bot completely. Every field matters.

### Full Schema

```jsonc
{
  // ── Identity ────────────────────────────────────────────────────────────────
  "name": "viper",
  // Short identifier. Used in log lines, DB rows, and state files.
  // Values used in production: hydra · viper · mamba · taipan · cobra · anaconda

  "model": "momentum_hunter",
  // Which ML model this bot runs. Must match a key in the strategy runner's
  // model registry. Determines which features are extracted from bars and which
  // candle key (m15 vs h1) is passed to the model.

  "timeframe": "m5",
  // Human-readable label. Used in logs and the ml_signals.timeframe column.
  // Does NOT control what bars are fetched — tf_enum does.

  "tf_enum": 5,
  // cTrader period enum passed to GetTrendbarsReq.
  // 1=M1  5=M5  7=M15  8=M30  9=H1  10=H4
  // Must match timeframe. If they diverge the label in the DB will be wrong.

  // ── Account ─────────────────────────────────────────────────────────────────
  "account_id": 12345678,
  // ctidTraderAccountId for this bot's dedicated cTrader demo account.
  // Every bot uses a separate account so position state never bleeds across bots.
  // The live account ID must be set in ML_FORBIDDEN_ACCOUNT_ID — the config
  // loader refuses to start if any bot matches it.

  // ── Sizing ──────────────────────────────────────────────────────────────────
  "lots": 0.05,
  // Fallback lot size. Used only when notional_pct is 0 or balance cannot be
  // fetched. For active trading keep notional_pct > 0 and treat lots as a floor.

  "notional_pct": 1.0,
  // Primary sizing control. Expressed as a fraction of account balance.
  // target_notional = balance × notional_pct × streak_mult
  // where streak_mult = clamp(0.5 + rolling_win_rate_last_10, 0.5, 1.5)
  //
  // A fresh account with no closed trades gets streak_mult = 1.0 (neutral).
  // A bot on a 10-trade win streak gets streak_mult = 1.5 (size up).
  // A bot on a 10-trade losing streak gets streak_mult = 0.5 (size down).
  //
  // The actual lot size is then derived from target_notional ÷ pip_value.
  // Set to 0.0 to disable adaptive sizing and fall back to lots.

  // ── Execution Gate ───────────────────────────────────────────────────────────
  "min_confidence": 0.62,
  // Minimum model confidence to open a trade. Signals below this threshold
  // are recorded to ml_signals but do not become orders.
  // Higher = fewer trades, higher selectivity. Lower = more trades, more noise.
  // This is the primary knob for signal quality vs. trade frequency trade-off.
  // Tuning this per asset per bot is where the edge lives.

  "max_concurrent": 1,
  // Maximum open trades this bot may hold for any single symbol at once.
  // 1 means the bot skips new signals for a symbol it already has a position in.
  // Increase only if the model is designed for pyramiding.

  // ── Data Collection ──────────────────────────────────────────────────────────
  "bar_count": 200
  // How many historical bars to fetch per symbol per evaluation cycle.
  // Larger = richer feature extraction but slower bar fetches.
  // Must be ≥ the longest lookback period used by the model.
}
```

### Other `.env` Variables

| Variable | Purpose |
|---|---|
| `ML_CTRADER_CLIENT_ID` | cTrader Open API app Client ID |
| `ML_CTRADER_CLIENT_SECRET` | cTrader Open API app Client Secret |
| `ML_CTRADER_ACCESS_TOKEN` | OAuth 2.0 access token (covers all demo accounts) |
| `ML_PRICE_FEED_ACCOUNT_ID` | Account used for bar fetching (defaults to first bot's account) |
| `ML_DATABASE_URL` | PostgreSQL DSN (`postgresql://user:pass@host:5432/db`) |
| `ML_SYMBOLS` | Comma-separated symbol list, e.g. `EURUSD,GBPUSD,USDJPY` |
| `ML_FORBIDDEN_ACCOUNT_ID` | Live account ID that the collector must never touch |
| `ML_LOOP_INTERVAL_SECONDS` | Seconds between bar evaluation cycles (default 60) |
| `ML_POSITION_POLL_SECONDS` | How often the monitor loop reconciles open positions (default 30) |
| `ML_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `ML_STATE_DIR` | Directory for per-bot state JSON files |

> **The exact values of `min_confidence`, `notional_pct`, `lots`, `bar_count`, and `ML_SYMBOLS` used in production are not stored in this repo.** That calibration is the moat.

---

## Data Collection

Three PostgreSQL tables capture everything:

### `ml_signals`
Every evaluation, every symbol, every bar close.

| Column | What it records |
|---|---|
| `bot_name` | Which bot evaluated |
| `model` | Which model |
| `symbol` | Instrument |
| `timeframe` | Timeframe label |
| `signal` | `BUY` / `SELL` / `HOLD` |
| `confidence` | Model output in [0, 1] |
| `features` | JSONB feature vector used by the model |
| `bar_time` | Bar close UTC timestamp |

### `ml_trades`
Every order opened and its eventual outcome.

| Column | What it records |
|---|---|
| `bot_name` | Which bot opened the trade |
| `symbol` | Instrument |
| `side` | `BUY` or `SELL` |
| `lots` | Executed lot size |
| `entry_price` | Fill price |
| `exit_price` | Closing fill price (populated by monitor loop via deal history API) |
| `sl` / `tp` | Stop loss and take profit levels |
| `outcome` | `WIN` / `LOSS` / `UNKNOWN` |
| `pnl` | Gross profit from cTrader `closePositionDetail` |
| `opened_at` / `closed_at` | UTC timestamps |
| `ticket` | cTrader positionId |
| `signal_confidence` | Confidence score that triggered the trade |

### `ml_bars`
OHLCV archive keyed by symbol and timeframe, used for offline model training.

---

## Adaptive Sizing

When `notional_pct > 0`, lot size is computed as:

```
streak_mult  = clamp(0.5 + rolling_win_rate(last 10 closed trades), 0.5, 1.5)
target_notional = balance × notional_pct × streak_mult
lots         = max(min_lots, target_notional / pip_value_per_lot)
```

This means:
- A bot on a losing streak automatically trades smaller.
- A bot on a winning streak gradually scales up, capped at 1.5×.
- Sizing always stays within `[min_lots, max_lots]` floor/ceiling defined in the sizer.

---

## Repo Layout

```text
glitch-ouroboros-snake-strategy/
├── mt5/
│   ├── bots/          # MT5 Expert Advisors (viper, cobra, taipan, mamba, anaconda, hydra)
│   ├── shared/        # Shared MT5 indicator and utility modules
│   └── configs/       # Sanitized example configs (no live values)
├── ctrader/
│   ├── executor/      # cTrader Open API async client + Protobuf helper
│   └── ml_collector/  # Full ML data collection engine
│       ├── collector.py       # Main async loop: bar fetch → signal → trade → monitor
│       ├── strategy_runner.py # Model evaluation per bot per symbol per bar
│       ├── sizer.py           # Adaptive lot sizing
│       ├── order_placer.py    # Trade execution via ctrader_client
│       ├── bar_fetcher.py     # Bar retrieval from cTrader API
│       ├── db.py              # PostgreSQL read/write layer
│       ├── config.py          # ML_BOTS schema + env loader
│       └── schema.sql         # Database schema
└── docs/
    ├── architecture.md
    ├── operating-model.md
    ├── engine.md          # Full technical reference
    └── platforms.md
```

---

## Quick Start

1. Read [Architecture](./docs/architecture.md) for the ensemble shape.
2. Read [Engine Reference](./docs/engine.md) for the full ML collector technical spec.
3. Read [Operating Model](./docs/operating-model.md) for Oracle coordination and bot responsibilities.
4. Review [mt5/README.md](./mt5/README.md) and [ctrader/README.md](./ctrader/README.md) for platform tracks.
5. Copy `ctrader/ml_collector/.env.example` to `.env` and fill in your own values.
6. Treat all configs in Git as sanitized examples — production values live outside the repo.

---

## What Is Not In This Repo

The public codebase contains the complete engine architecture. What it does not contain:

- **Production `min_confidence` values per bot per symbol** — the primary calibration
- **Production `notional_pct` / lot sizes** — the sizing calibration
- **Production symbol list** — which instruments actually run
- **Trained model weights** — the ML artefacts themselves
- **Live `.env` files** — credentials and runtime values
- **Trade history and signal logs** — the 6-month data accumulation

The engine is the skeleton. The calibration is the edge.

---

## Part Of The Glitch Ecosystem

- [Glitch Trading Core](https://github.com/glitch-exec-labs/glitch-trading-core) — umbrella architecture repo
- [Indian King Cobra](https://github.com/glitch-exec-labs/glitch-indian-king-cobra) — standalone unified momentum scalper
- [Terciopelo](https://github.com/glitch-exec-labs/glitch-terciopelo) — standalone equities relative-value strategy

---

## Public Repo Safety

- Only sanitized example configs are included
- No live credentials, state, models, logs, or training data are committed
- Secrets must live outside Git

---

## Contributing

The best public contributions are documentation clarity, sanitized examples, and platform-portability improvements that keep the ensemble design understandable. Start with [CONTRIBUTING.md](./CONTRIBUTING.md).

---

## Documentation

- [Architecture](./docs/architecture.md)
- [Engine Reference](./docs/engine.md)
- [Operating Model](./docs/operating-model.md)
- [Platforms](./docs/platforms.md)
- [MT5 Track](./mt5/README.md)
- [cTrader Track](./ctrader/README.md)

---

## Maintainer And Contact

Glitch Executor is developed and maintained by Tejas Karan Agrawal, operating under the business name Nuraveda.

- Support and responsible disclosure: `support@glitchexecutor.com`
- Registered address: `77 Huntley St, Toronto, ON M4Y 2P3, Canada`

---

## License

Released under [Apache 2.0](./LICENSE) with attribution preserved through [NOTICE](./NOTICE).
