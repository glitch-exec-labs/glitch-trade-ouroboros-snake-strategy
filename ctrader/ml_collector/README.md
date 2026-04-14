# Glitch ML Data Collector

Clean-slate research pipeline for the trading strategies. Runs momentum
+ mean-reversion bots on **fresh cTrader demo accounts**, logs every
signal + trade + outcome to daily CSV files, and pushes them to a
companion ml-data repo under `ml_data_clean/` once a day.

**This is a research data collector, not a trading bot.** It is hard-isolated
from the production stack: dedicated Linux user, separate venv, separate `.env`,
no Docker touches, no DB writes, `live=False` hardcoded.

---

## Why this exists

The previous `ml_data/` folder in the companion ml-data repo accumulated
development-phase noise — many signals with no executed trades, and a
separate set of closed trades from prop/demo experiments. Rather than train
on mixed data, we start over with a single clean source of truth.

After a sustained run of continuous operation on clean demo accounts, we'll
have statistically meaningful, git-versioned performance data that answers
the only question that matters: **does the edge actually exist?**

---

## Architecture

Two systemd units:

- **`glitch-ml-collector.service`** — long-running daemon
  - Every 60s: fetch cTrader candles → run King Cobra (momentum) on XAU + Mamba (mean-rev) on EUR → write CSV row → maybe place a trade
  - Every 30s: poll open positions → detect closures → update the row's outcome columns
- **`glitch-ml-gitsync.timer`** → `glitch-ml-gitsync.service`
  - Fires daily at 00:05 UTC
  - `git pull --rebase` → `git add ml_data_clean` → `git commit` → `git push` (3 retries)

All CSV writes are crash-safe: `fsync` on append, `tempfile + os.replace` on update,
`fcntl.flock` across the board.

---

## Safety rails (why this cannot touch production)

1. Dedicated Linux user — cannot read the production platform's `.env`
2. Separate venv at `ml_collector/venv/` — cannot accidentally use production site-packages
3. Separate `.env` at `ml_collector/.env` — `config.py` **only** loads this file
4. `ML_CTRADER_*` env vars → proxied to `CTRADER_*` with `CTRADER_LIVE=false` forced
5. `CTraderClient(..., live=False)` hardcoded in `collector.py`
6. Runtime assertion: account ID must not match the production live account
   (configured via `_FORBIDDEN_LIVE_ACCOUNT_ID` in `config.py`); collector
   refuses to start otherwise
7. `systemd` `ReadWritePaths=` limits filesystem writes to two paths only
8. `git add ml_data_clean` — explicit path, **never** `git add -A`

---

## Bootstrap (first-time install)

Server-side paths, user names, and companion-repo names are environment-specific
and documented in private ops runbooks — not here. The outline of the
bootstrap is:

1. Create a dedicated Linux user for the collector.
2. Generate an SSH deploy key for the ml-data companion repo; register it as a
   write key on the companion repo.
3. Clone the ml-data companion repo as the dedicated user.
4. Create a venv inside this `ml_collector/` directory and
   `pip install -r ../requirements.txt`.
5. Create a fresh Spotware demo account and register an app at
   <https://openapi.ctrader.com> to obtain OAuth credentials.
6. Copy `.env.example` to `.env` and fill in the credentials. Set
   `ML_FORBIDDEN_ACCOUNT_ID` to your production live account ID as a
   safety net.
7. Run the smoke tests (`python -m ml_collector.tests.test_smoke`).
8. Install the systemd unit from `ctrader/systemd/` and enable it.
9. Enable the git-sync timer.
10. Manually trigger a first git sync and verify a commit lands on GitHub.

---

## CSV Schema

One row per evaluation. The row starts with signal + bar data, gets entry/sl/tp
filled in if the strategy executes, and gets the exit columns filled in when
the position closes.

Columns (in order):
```
row_id, timestamp, strategy, symbol, bot, account, timeframe,
signal, signal_type, confidence, reasoning,
executed, entry_price, sl_price, tp_price, volume_lots, ticket,
exit_price, exit_reason, profit, pnl, outcome, duration_minutes,
account_balance, account_equity,
bar_open, bar_high, bar_low, bar_close,
rsi, ema_20, price_above_ema, volume_ratio, rsi_crossover,
bb_upper, bb_mid, bb_lower, bb_width, price_position_in_bb,
adx, atr, regime, trigger,
indicators_json
```

`row_id` is a UUID and the primary key — used to locate and update rows when
positions close.

---

## File layout in the companion ml-data repo

```
<ml-data repo>/
├── ml_data/                        # legacy data, untouched
└── ml_data_clean/                  # this collector writes here
    ├── king_cobra/
    │   └── king_cobra_signals_YYYY-MM-DD.csv
    └── mamba/
        └── mamba_signals_YYYY-MM-DD.csv
```

---

## Important caveats (read before trusting the data)

1. **Demo fills are idealized.** No slippage, no requotes, no partial fills.
   Performance on a clean demo is an **upper bound** on what a live account
   would achieve. Do not mix demo CSVs with future live CSVs in ML training.
2. **Closure classification is approximate.** We infer tp_hit / sl_hit / manual
   by comparing the m15 close to the stored SL/TP at the moment the ticket
   disappears from the reconcile list. cTrader Open API does not expose
   historical deal records cheaply, so some rows will have
   `outcome=UNKNOWN` or `exit_reason=manual_or_unknown`.
3. **PnL in the CSV is coarse.** It uses a generic contract multiplier.
   The authoritative P&L always lives in cTrader's books. Use the CSV for
   win/loss distribution analysis, not P&L audits.
4. **Market-hours gate is hardcoded.** Closed all Sat, Sun until 22:00 UTC,
   Fri after 21:00 UTC. No holiday calendar.
5. **Mamba uses `closes[-2]` (prior closed bar).** At a 60s loop cadence the
   same M15 bar repeats for ~15 minutes, so most HOLD rows look identical.
   The CSV writer deduplicates consecutive HOLD rows on bar_close.

---

## Operations

- **Logs:** `journalctl -u glitch-ml-collector -f`
- **Next git sync:** `systemctl list-timers glitch-ml-gitsync.timer`
- **Last git sync:** `journalctl -u glitch-ml-gitsync -n 200`
- **Tracked open positions:** `cat <collector_dir>/state/open_trades.json`
- **PID lock file:** `cat <collector_dir>/state/collector.pid`
- **Force a git sync now:** `sudo systemctl start glitch-ml-gitsync.service`
- **Manual rebuild from scratch:** stop the service, remove
  `state/open_trades.json`, restart.

## Revoking access

1. Disable and remove the two systemd units, then `daemon-reload`.
2. Remove the dedicated Linux user.
3. Revoke the deploy key on the companion ml-data repo's settings page.
