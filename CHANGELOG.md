# Changelog

All notable changes to the Ouroboros Snake Strategy project are logged here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed — 2026-04-14 · ProtoOATraderReq/Res wrong payload type

- `executor/ctrader_client.py` declared `PT_TRADER_REQ = 2104` and
  `PT_TRADER_RES = 2105`. Those are a different cTrader message
  entirely. The request went out with the wrong payloadType and the
  reply was parsed into an empty `ProtoOATraderRes`, so `get_balance`
  silently returned zeros. Correct constants are 2121 / 2122 — matches
  what `ml_collector/sizer.py` has always used (which is why adaptive
  sizing showed real balances while the closure path stored zeros).
- Effect on stored data: every closed `ml_trades` row up to this fix
  had `account_balance = 0` and `account_equity = 0`. Future closures
  will populate correctly.

### Fixed — 2026-04-14 · ProtoOAPosition.unrealizedPnl noise

- `CTraderClient._reconcile()` was reading `pos.unrealizedPnl`, a field
  that does not exist on `ProtoOAPosition` in the cTrader Open API schema
  (the spec returns only `commission`, `swap`, `usedMargin`, `marginRate`
  and a few flags — P&L must come from `ProtoOADealListReq`). Every poll
  cycle logged `get_open_positions failed: Protocol message
  ProtoOAPosition has no non-repeated field "unrealizedPnl"`. Drop the
  field and set `profit=0` — callers only need the ticket list for
  existence checking.

### Changed — 2026-04-14 · Per-timeframe notional_pct defaults

Tiered `notional_pct` by bot timeframe so faster bots don't crowd the
book with oversized positions. Runtime `.env` values (not committed):

| Bot | TF | notional_pct |
|-----|-----|--------------|
| hydra | m1 | 0.1 |
| viper | m5 | 0.2 |
| mamba | m15 | 0.3 |
| taipan | m30 | 0.5 |
| cobra | h1 | 0.7 |
| anaconda | h4 | 1.0 |

### Fixed — 2026-04-14 · JPY undersizing + outcome classifier

- **JPY-quoted symbols (EURJPY, USDJPY, GBPJPY, JPN225) were being sized
  at the minimum volume.** The sizer formula assumed USD-quote currency; raw
  JPY prices (~187 for EURJPY, ~57,800 for JPN225) produced wire volumes
  100×+ too small, which then clamped up to `spec.min_volume`.
  - `compute_adaptive_lots()` now accepts `fx_rate_to_usd` (default 1.0).
  - `collector._execute_trade` passes the current USDJPY close (via a new
    shared `latest_close_by_symbol` cache updated by `evaluate_symbol`) for
    JPY-quoted symbols; defaults to 150.0 if USDJPY hasn't been fetched yet.
- **All closed trades were being classified as `outcome=UNKNOWN`.**
  The monitor loop called `fetcher.fetch(symbol, "m15", 2)` for the current
  price, but `BarFetcher.fetch` returns `None` whenever `len(bars) < 50`,
  so `current` was always `None`. Now reads from `latest_close_by_symbol`
  (populated by the main eval loop); falls back to a proper 100-bar fetch
  on cache miss.

### Added — 2026-04-14 · ctrader/ track goes live

- **ctrader/ subtree** now holds the live six-snake execution stack previously
  managed outside this repo.
  - `ctrader/ml_collector/` — 6 bots (hydra m1, viper m5, mamba m15, taipan m30,
    cobra h1, anaconda h4) each bound to its own cTrader Open API demo account.
  - `ctrader/executor/` — vendored cTrader Open API client.
  - `ctrader/ensemble/` — vendored price feed + 7 strategy models.
  - `ctrader/systemd/glitch-ml-collector.service` — deploy unit.
  - `ctrader/requirements.txt` — pinned to known-working set (ctrader-open-api
    0.9.0, protobuf 3.19.1, Twisted 21.7.0) to avoid dependency resolver conflict.
- **Adaptive position sizer** (`ctrader/ml_collector/sizer.py`).
  Each trade's lot size derives from `balance × notional_pct × streak_mult`:
  - `balance` fetched live from cTrader (60s cache via `BalanceCache`).
  - `notional_pct` per-bot config, default `1.0` (100% of equity notional).
  - `streak_mult` maps rolling win-rate of last 10 closed trades to a
    `[0.5, 1.5]` multiplier — hot streaks compound faster than equity growth,
    cold streaks halve size.
  - Snaps to each symbol's `min_volume`/`step_volume`.
- **GitHub Actions auto-deploy** (`.github/workflows/deploy-ctrader.yml`).
  Push to `main` touching `ctrader/**` → SSH → `git pull` →
  `systemctl restart glitch-ml-collector.service`. No manual steps.

### Fixed — 2026-04-14 · cTrader order placement bugs

Three bugs were preventing MARKET orders from being accepted by Spotware.
All three fixed in `ctrader/ml_collector/order_placer.py`:

1. **`lots_to_wire` was off by 100×** — the formula was
   `lots × lot_size / 100` but should be `lots × lot_size`. A config of
   `lots=1.0` was placing 0.01 lots. Trades now hit the configured size.
2. **MARKET orders rejected with "SL/TP in absolute values are allowed only
   for order types: [LIMIT, STOP, STOP_LIMIT]"** — switched from
   `stopLoss`/`takeProfit` (absolute prices, LIMIT/STOP only) to
   `relativeStopLoss`/`relativeTakeProfit` (1/100000 price-unit offsets,
   required for MARKET orders).
3. **"Relative stop loss has invalid precision" on 3-digit JPY pairs and
   1-digit indices** — round relative SL/TP to the symbol's precision step
   (`10 ** (5 - digits)`) so the resulting price lands on a valid tick.

### Migration path (server, one-time)

- Cloned repo to a new deploy location under a dedicated service user.
- Seeded `.env` + `state/` from the previous location (excluded from git
  via `.gitignore`).
- Built fresh venv, installed pinned `requirements.txt`.
- Replaced the systemd unit with the one from `ctrader/systemd/`,
  `daemon-reload`, `restart`.
- Added passwordless sudo rules for the deploy user (narrow scope: one git
  pull command + one systemctl restart command).
- Archived old location as a dated backup.

### Known issues (follow-up work)

- JPY-quoted pairs (EURJPY, USDJPY, GBPJPY) and JPY-quoted indices (JPN225)
  are undersized because the sizer uses raw JPY price without USDJPY
  conversion. Result: ~0.01 lots instead of ~0.4 lots.
- The position outcome classifier in `position_tracker._classify` is marking
  all closed trades as `UNKNOWN` instead of `WIN`/`LOSS`, which keeps
  `streak_mult` pinned at 1.0 (no adaptive streak effect, just plain
  compounding from live balance).

---

## [0.1.0] — 2026-04-11

Initial public release of the Ouroboros Snake Strategy.

- MT5 bot stack under `mt5/bots/` (anaconda, cobra, hydra, mamba, taipan,
  viper + oracle coordinator + news guard).
- Documentation under `docs/` (architecture, operating model, platforms).
- Empty `ctrader/` placeholder awaiting the cTrader execution track.
