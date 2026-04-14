# Glitch ML Data Collector

Clean-slate research pipeline for the Glitch Executor trading strategies.
Runs momentum + mean-reversion bots on a **fresh cTrader demo account**,
logs every signal + trade + outcome to daily CSV files, and pushes them
daily to the `glitch-executor-ml-data` GitHub repo under `ml_data_clean/`.

**This is a research data collector, not a trading bot.** It is hard-isolated
from the production stack: dedicated Linux user, separate venv, separate `.env`,
no Docker touches, no DB writes, `live=False` hardcoded.

---

## Why this exists

The existing `ml_data/` folder in the ml-data repo is polluted with
development-phase data — 712,967 signals with zero executed trades,
plus ~230 closed trades aggregating to −$28K P&L across prop/demo experiments.
Rather than build customer-facing products on that data, we start over with
a single clean source of truth.

After ~30 days of continuous operation, we'll have statistically meaningful,
git-versioned performance data that answers the only question that matters:
**does the edge actually exist on a clean account?**

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

1. Dedicated Linux user `glitchml` — cannot read `/opt/glitchexecutor/.env`
2. Separate venv at `ml_collector/venv/` — cannot accidentally use production site-packages
3. Separate `.env` at `ml_collector/.env` — `config.py` **only** loads this file
4. `ML_CTRADER_*` env vars → proxied to `CTRADER_*` with `CTRADER_LIVE=false` forced
5. `CTraderClient(..., live=False)` hardcoded in `collector.py`
6. Runtime assertion: `account_id != 46868136` — collector refuses to start if it matches the production live account
7. `systemd` `ReadWritePaths=` limits filesystem writes to two paths only
8. `git add ml_data_clean` — explicit path, **never** `git add -A`

---

## Bootstrap (first-time install)

```bash
# 1. Create dedicated user
sudo useradd -r -m -d /home/glitchml -s /bin/bash glitchml

# 2. Generate SSH deploy key for the ml-data repo
sudo -u glitchml ssh-keygen -t ed25519 -f /home/glitchml/.ssh/id_ed25519 -N ""
sudo cat /home/glitchml/.ssh/id_ed25519.pub
# Add the above as a WRITE deploy key on github.com/glitch-executor/glitch-executor-ml-data

# 3. Clone the data repo (use ssh, not https)
sudo mkdir -p /opt/glitch-ml-data
sudo chown glitchml:glitchml /opt/glitch-ml-data
sudo -u glitchml git clone git@github.com:glitch-executor/glitch-executor-ml-data.git /opt/glitch-ml-data

# 4. Give glitchml write access to the collector dir + state dir
sudo chown -R glitchml:glitchml /opt/glitchexecutor/ml_collector

# 5. Create venv + install deps
sudo -u glitchml python3 -m venv /opt/glitchexecutor/ml_collector/venv
sudo -u glitchml /opt/glitchexecutor/ml_collector/venv/bin/pip install \
  -r /opt/glitchexecutor/ml_collector/requirements.txt

# 6. Create a FRESH Spotware demo account + cTrader API app, get credentials
#    https://ct.spotware.com — new demo account
#    https://openapi.ctrader.com — create app, generate OAuth token for the demo
#    CRITICAL: must not be account 46868136

# 7. Fill in .env
sudo -u glitchml cp /opt/glitchexecutor/ml_collector/.env.example \
                    /opt/glitchexecutor/ml_collector/.env
sudo -u glitchml nano /opt/glitchexecutor/ml_collector/.env
sudo chmod 600 /opt/glitchexecutor/ml_collector/.env

# 8. Smoke test — prints signals for both strategies, no trade, no CSV
sudo -u glitchml /opt/glitchexecutor/ml_collector/venv/bin/python \
  -m ml_collector.tests.test_smoke

# 9. Dry-run CSV write (writes to /tmp/ml_smoke)
sudo -u glitchml /opt/glitchexecutor/ml_collector/venv/bin/python \
  -m ml_collector.tests.test_smoke --write --dir /tmp/ml_smoke

# 10. Install systemd units
sudo cp /opt/glitchexecutor/ml_collector/systemd/glitch-ml-collector.service /etc/systemd/system/
sudo cp /opt/glitchexecutor/ml_collector/systemd/glitch-ml-gitsync.service /etc/systemd/system/
sudo cp /opt/glitchexecutor/ml_collector/systemd/glitch-ml-gitsync.timer /etc/systemd/system/
sudo systemctl daemon-reload

# 11. Start the collector
sudo systemctl enable --now glitch-ml-collector.service
sudo systemctl status glitch-ml-collector
journalctl -u glitch-ml-collector -f

# 12. Enable the git-sync timer
sudo systemctl enable --now glitch-ml-gitsync.timer
sudo systemctl list-timers | grep glitch-ml

# 13. Manual first git sync — verify a commit lands on GitHub before going to sleep
sudo systemctl start glitch-ml-gitsync.service
sudo journalctl -u glitch-ml-gitsync -n 50
```

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

## File layout in the ml-data repo

```
/opt/glitch-ml-data/
├── ml_data/                        # existing polluted data, untouched
└── ml_data_clean/                  # NEW — this collector writes here
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
- **Tracked open positions:** `cat /opt/glitchexecutor/ml_collector/state/open_trades.json`
- **PID lock file:** `cat /opt/glitchexecutor/ml_collector/state/collector.pid`
- **Force a git sync now:** `sudo systemctl start glitch-ml-gitsync.service`
- **Manual rebuild from scratch:** `sudo systemctl stop glitch-ml-collector && rm -f state/open_trades.json && sudo systemctl start glitch-ml-collector`

## Revoking access

To completely disable and clean up:

```bash
sudo systemctl disable --now glitch-ml-collector.service glitch-ml-gitsync.timer
sudo rm /etc/systemd/system/glitch-ml-{collector,gitsync}.{service,timer}
sudo systemctl daemon-reload
sudo userdel -r glitchml
# Revoke the deploy key on github.com/glitch-executor/glitch-executor-ml-data/settings/keys
```
