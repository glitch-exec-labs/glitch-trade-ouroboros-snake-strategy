# Engine Reference: ML Collector

This document is the full technical reference for the Ouroboros ML data collection engine — the layer that runs six bots in parallel, evaluates signals, executes trades on cTrader demo accounts, and records every outcome to PostgreSQL.

---

## Purpose

The ML collector exists to accumulate structured training data: what signal the model generated, what the confidence was, whether a trade was opened, and what the outcome was. After six months of this data, the models can be retrained on real in-broker observations rather than backtested simulations.

It is not a live production trading system. It runs on demo accounts. Every position it opens is part of a labelled dataset.

---

## Process Architecture

```
glitch-ml-collector.service     (systemd, always-on)
    └── python -m ml_collector
            ├── 6 × bot_loop()          (one per bot, staggered 10 s apart)
            │       ├── bar_fetcher     (cTrader GetTrendbarsReq per symbol)
            │       ├── strategy_runner (model evaluation)
            │       ├── db.write_signal (→ ml_signals)
            │       ├── confidence gate
            │       ├── sizer           (adaptive lot calculation)
            │       └── order_placer    (cTrader NewOrderReq → ml_trades)
            └── monitor_loop()          (polls every 30 s)
                    ├── ctrader_client.get_open_positions()
                    ├── reconcile against ml_trades WHERE closed_at IS NULL
                    ├── detect closures
                    └── ctrader_client.get_deals_by_position()
                            └── db.close_trade (exit_price, pnl, outcome → ml_trades)

glitch-ml-export.service        (systemd, triggered by timer at 00:10 UTC)
    └── export_daily.py
            ├── SELECT yesterday FROM ml_signals → CSV
            └── SELECT yesterday FROM ml_trades  → CSV
```

---

## Bot Loop Detail

Each of the six bots runs `bot_loop(bot_config)` as an asyncio task. They are staggered 10 seconds apart at startup to spread cTrader API load.

Every `loop_interval_seconds` (default 60 s, configurable), the bot:

1. **Fetches bars** — calls `bar_fetcher.get_bars(symbol, tf_enum, bar_count)` for every symbol in `ML_SYMBOLS`. This calls `ProtoOAGetTrendbarsReq` on the bot's dedicated demo account.

2. **Evaluates the model** — passes the bar array to `strategy_runner.evaluate(bot, bars)`. The runner:
   - Extracts features from the bars
   - Derives the correct candle key (`m15` or `h1`) based on `_MODEL_CANDLE_KEY[bot.model]`
   - Calls the model's `predict(candles)` method
   - Returns `(signal, confidence, features)` where signal is `BUY` / `SELL` / `HOLD`

3. **Records the signal** — writes every evaluation (including HOLDs) to `ml_signals`. This is the raw labelling dataset.

4. **Applies the confidence gate** — skips execution if:
   - `signal == HOLD`
   - `confidence < bot.min_confidence`
   - The bot already has an open trade for this symbol (`max_concurrent` check against `ml_trades`)

5. **Sizes the order** — calls `sizer.compute_lots(bot, client)`. See [Adaptive Sizing](#adaptive-sizing) below.

6. **Places the order** — calls `order_placer.place(bot, symbol, signal, lots)` which calls `CTraderClient.place_order()`. On success, writes the entry to `ml_trades`.

---

## Monitor Loop Detail

`monitor_loop()` runs as a separate asyncio task, polling every `position_poll_interval_seconds` (default 30 s).

Each cycle:

1. Loads all `ml_trades` where `closed_at IS NULL` (open trades in the DB).
2. Calls `CTraderClient.get_open_positions()` for each bot's account (using `ReconcileReq`).
3. Compares: any DB trade whose `ticket` (positionId) is absent from the live position list has been closed.
4. For each detected closure:
   - Calls `get_deals_by_position(ticket, from_ts_ms, to_ts_ms)` using `ProtoOADealListByPositionIdReq`
   - Finds the closing deal (the one with `closePositionDetail`)
   - Reads `executionPrice`, `grossProfit`, and `balance` from the deal
   - Determines `outcome`: `WIN` if `pnl > 0`, `LOSS` if `pnl < 0`, `UNKNOWN` if the deal was not found
   - Updates `ml_trades` with `exit_price`, `pnl`, `outcome`, `closed_at`

Using the deal history API (rather than estimating from the current bar price) gives authoritative broker-side execution prices and P&L.

---

## cTrader Client

`executor/ctrader_client.py` — async TCP client for the cTrader Open API.

**Transport:** asyncio streams over SSL. No Twisted. Each public method opens a fresh connection, authenticates at app level and account level, executes its operation, then closes.

**Protobuf:** uses the vendored `Protobuf` helper from spotware/OpenApiPy (`executor/protobuf.py`). This dynamic registry resolves any `ProtoOA*` class by abbreviated name (e.g. `"NewOrderReq"` → `ProtoOANewOrderReq`). No manual `PT_*` payload type constants. Future cTrader proto additions work automatically after `pip install -U ctrader-open-api`.

**Message framing:** 4-byte big-endian length prefix + `ProtoMessage` envelope. Standard cTrader wire format.

**Key public methods:**

| Method | Proto message used |
|---|---|
| `get_balance()` | `TraderReq` / `TraderRes` |
| `place_order()` | `NewOrderReq` / `ExecutionEvent` |
| `get_open_positions()` | `ReconcileReq` / `ReconcileRes` |
| `close_position()` | `ClosePositionReq` / `ExecutionEvent` |
| `modify_position()` | `AmendPositionSLTPReq` / `ExecutionEvent` |
| `get_deals_by_position()` | `DealListByPositionIdReq` / `DealListByPositionIdRes` |

**Enum values (cTrader Open API):**
- `tradeSide`: BUY = 1, SELL = 2
- `orderType`: MARKET = 1, LIMIT = 2
- `volume`: centilots (1 lot = 100, 0.01 lot = 1)
- `balance` and `grossProfit`: divided by `10^moneyDigits` (usually 2)

---

## Adaptive Sizing

When `notional_pct > 0`:

```
win_rate     = wins / max(closed_trades_last_10, 1)
streak_mult  = clamp(0.5 + win_rate, 0.5, 1.5)
target_notional = account_balance × notional_pct × streak_mult
lots         = clamp(target_notional / pip_value_per_lot, min_lots, max_lots)
```

Behaviour:
- **No trade history** (fresh account): `streak_mult = 1.0`
- **10/10 wins**: `streak_mult = 1.5` → size up 50%
- **0/10 wins**: `streak_mult = 0.5` → size down 50%
- **5/10 wins**: `streak_mult = 1.0` → neutral

The `notional_pct` per bot is the primary sizing lever. Setting it too high on a short-timeframe bot (e.g. hydra on M1) means the account drawdown during the data collection phase can be large. The purpose is labelled data, not profit — size accordingly.

---

## State Files

Each bot writes a JSON state file to `ML_STATE_DIR` (default `ctrader/ml_collector/state/`). These track the last bar time evaluated per symbol, preventing duplicate signals across restarts.

State files are not committed to Git (`.gitignore`).

---

## Database Schema

See `ctrader/ml_collector/schema.sql` for the full DDL.

Key design choices:
- `ml_signals` has no foreign key to `ml_trades` — signals and trades are independent records. A signal that does not pass the confidence gate still lands in `ml_signals`.
- `ml_trades.ticket` is the cTrader `positionId` — used to reconcile against the live account.
- `ml_trades.signal_confidence` is snapshotted at trade open — so model calibration can be evaluated against actual outcomes later.
- `ml_bars` stores raw OHLCV, keyed `(symbol, timeframe, bar_time)` with a unique constraint, so re-fetching the same bars is idempotent.

---

## Daily Export

`export_daily.py` (triggered by `glitch-ml-export.timer` at 00:10 UTC) exports the previous calendar day's rows from `ml_signals` and `ml_trades` to CSV. These CSVs are the input to offline model training pipelines.

---

## Candle Key Routing

The most operationally critical detail in `strategy_runner.py`:

```python
_MODEL_CANDLE_KEY: Dict[str, str] = {
    "momentum_hunter": "m15",
    "mamba_reversion":  "m15",
    "mean_reverter":    "m15",
    "trend_follower":   "h1",
    "volume_profiler":  "h1",
    "session_analyst":  "h1",
    "multi_tf_align":   "m15",
}
```

Models that expect `candles["h1"]` (trend_follower, volume_profiler, session_analyst) will silently return HOLD if the runner hands them `{"m15": bars}`. This mapping ensures each model receives the timeframe its feature extraction expects. If you add a new model, define its candle key here first.

---

## Adding A New Bot

1. Create a new cTrader demo account. Note its `ctidTraderAccountId`.
2. Add a new entry to `ML_BOTS` in the runtime `.env`.
3. If the bot uses a new model, add the model's candle key to `_MODEL_CANDLE_KEY` in `strategy_runner.py`.
4. Restart `glitch-ml-collector.service`.

No code changes are required for new bots running existing models.

---

## Platform Notes

- **Current:** cTrader Open API (protobuf, TCP/SSL, port 5035)
- **Price feed account:** a single cTrader account is used for bar fetching (`ML_PRICE_FEED_ACCOUNT_ID`). Trade execution uses each bot's dedicated account.
- **Demo only:** `live=False` is hardcoded in the ML collector config. The `ML_FORBIDDEN_ACCOUNT_ID` guard provides a second layer of protection.
- **MT5 track:** the `mt5/` directory contains Expert Advisors for the same six bots running on MT5. The cTrader ML collector is the primary data accumulation path going forward.
