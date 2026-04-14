# cTrader Track

Live six-snake execution stack on cTrader Open API. Runs on a Linux server under systemd, collects ML training data to PostgreSQL, and auto-restarts on every push to `main`.

## Layout

```
ctrader/
├── ml_collector/     # the 6-bot runtime (hydra m1, viper m5, mamba m15,
│                     #                    taipan m30, cobra h1, anaconda h4)
├── executor/         # vendored cTrader client (ProtoOANewOrderReq wiring)
├── ensemble/         # vendored price feed + 7 strategy models
├── systemd/          # deploy unit
├── requirements.txt
└── README.md
```

## Bot-to-account mapping (Pepperstone demo, $50K each)

| Bot | Model | TF | Account |
|-----|-------|-----|---------|
| hydra | mamba_reversion | m1 | 5267354 |
| viper | momentum_hunter | m5 | 5267327 |
| mamba | mamba_reversion | m15 | 5267330 |
| taipan | session_analyst | m30 | 5267355 |
| cobra | trend_follower | h1 | 5267329 |
| anaconda | volume_profiler | h4 | 5267391 |

## Adaptive position sizing

`ml_collector/sizer.py` computes lots adaptively on each trade:

```
lots = (balance × notional_pct × streak_mult) / (price × 0.01)
```

- `balance`: live from cTrader API (60s cache)
- `notional_pct`: per-bot config, default 1.0 = 100% of equity notional
- `streak_mult`: 0.5× → 1.5× based on rolling win rate of last 10 closed trades

Win streaks grow sizing; loss streaks shrink it. Compounding is automatic.

## Deploy

Push to `main` → GitHub Actions SSHes to the server → `git pull` → `systemctl restart glitch-ml-collector.service`. No manual steps after initial provisioning.

## Operations

```bash
systemctl status glitch-ml-collector.service
journalctl -u glitch-ml-collector.service -f | grep "TRADE "
psql $ML_DATABASE_URL -c "SELECT COUNT(*) FROM ml_trades WHERE closed_at IS NULL;"
```

ML data (CSVs, models) is **not** part of this repo — it lives at `/opt/glitch-ml-data/` on the server and syncs to `glitch-exec-labs/glitch-executor-ml-data` daily.
