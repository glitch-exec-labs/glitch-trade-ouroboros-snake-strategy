"""
One-shot stale-trade reconciler.

Walks every ml_trades row WHERE closed_at IS NULL, groups by account, and
for each account asks cTrader what positions are ACTUALLY open right now.
Any DB-open trade whose ticket is absent from cTrader's ReconcileRes is
closed in the DB.

For each trade being closed we try ProtoOADealListByPositionIdReq to get
the authoritative exit price and PnL from the broker. If deal history is
expired (demo servers retain only a few days), we fall back to
outcome=UNKNOWN, pnl=0, exit_reason='broker_closed_stale'.

Runs once and exits. Safe to run repeatedly — idempotent.

Usage:
    cd /opt/glitch-ouroboros/ctrader
    sudo -u glitchml ml_collector/venv/bin/python -m ml_collector.reconcile_stale
    # Optional: --dry-run to only report, not update.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import asyncpg

from . import _ctrader_compat  # noqa: F401 — must precede ctrader_client import
from executor.ctrader_client import CTraderClient

from .config import get_config, configure_logging
from .db import DatabaseWriter

logger = logging.getLogger("ml_collector.reconcile_stale")


def _classify_pnl(pnl: float) -> str:
    if pnl > 0:
        return "WIN"
    if pnl < 0:
        return "LOSS"
    return "UNKNOWN"


async def reconcile(dry_run: bool = False) -> None:
    cfg = get_config()
    configure_logging(cfg)
    logger.info("Starting stale-trade reconciler (dry_run=%s)", dry_run)

    pool = await asyncpg.create_pool(cfg.db_dsn, min_size=1, max_size=4)
    db = DatabaseWriter(pool)

    # Group open DB trades by account_id
    open_rows = await pool.fetch(
        "SELECT id, bot_name, symbol, account_id, side, ticket, volume_lots, "
        "       entry_price, opened_at "
        "FROM ml_trades WHERE closed_at IS NULL AND ticket IS NOT NULL "
        "ORDER BY account_id, opened_at"
    )
    if not open_rows:
        logger.info("No DB-open trades found. Nothing to reconcile.")
        await pool.close()
        return

    by_account: Dict[int, List[dict]] = defaultdict(list)
    for r in open_rows:
        by_account[int(r["account_id"])].append(dict(r))
    logger.info("Loaded %d DB-open trades across %d accounts",
                len(open_rows), len(by_account))

    totals = {"kept_open": 0, "closed_with_deal": 0, "closed_stale": 0, "errors": 0}

    for account_id, db_trades in by_account.items():
        client = CTraderClient(
            client_id=cfg.ctrader_client_id,
            client_secret=cfg.ctrader_client_secret,
            access_token=cfg.ctrader_access_token,
            account_id=account_id,
            live=False,
        )

        # 1. What's actually open at the broker?
        try:
            live_positions = await client.get_open_positions()
        except Exception:
            logger.exception("ReconcileRes failed for account %d — skipping", account_id)
            totals["errors"] += len(db_trades)
            continue

        live_tickets = {str(p["ticket"]) for p in live_positions}
        logger.info("Account %d: %d DB-open, %d broker-open",
                    account_id, len(db_trades), len(live_tickets))

        # 2. Balance snapshot once per account (for close_trade bookkeeping)
        try:
            bal = await client.get_balance()
            acc_balance = float(bal.get("total", 0) or 0)
            acc_equity  = float(bal.get("equity", 0) or 0)
        except Exception:
            acc_balance = acc_equity = 0.0

        # 3. Per-trade reconcile
        for t in db_trades:
            ticket = str(t["ticket"])
            if ticket in live_tickets:
                totals["kept_open"] += 1
                continue

            # Broker-closed. Try deal history for authoritative PnL.
            from_ts_ms = int(t["opened_at"].replace(tzinfo=timezone.utc).timestamp() * 1000) \
                         if t["opened_at"].tzinfo is None \
                         else int(t["opened_at"].timestamp() * 1000)
            to_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000
            try:
                deals = await client.get_deals_by_position(ticket, from_ts_ms, to_ts_ms)
            except Exception:
                logger.exception("get_deals_by_position failed for %s", ticket)
                deals = []

            closing = next((d for d in deals if d.get("is_close")), None)

            if closing and closing.get("execution_price"):
                exit_price    = float(closing["execution_price"])
                pnl           = float(closing.get("gross_profit", 0.0))
                close_bal     = float(closing.get("balance", acc_balance) or acc_balance)
                outcome       = _classify_pnl(pnl)
                exit_reason   = "broker_closed"
                totals["closed_with_deal"] += 1
            else:
                # Deal history expired (older than demo retention window).
                exit_price    = float(t["entry_price"] or 0)
                pnl           = 0.0
                close_bal     = acc_balance
                outcome       = "UNKNOWN"
                exit_reason   = "broker_closed_stale"
                totals["closed_stale"] += 1

            opened  = t["opened_at"]
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            duration_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0

            logger.info(
                "%s %s %s tkt=%s → %s pnl=%.2f exit=%.5f reason=%s (held %.1fm)",
                t["bot_name"], t["symbol"], t["side"], ticket,
                outcome, pnl, exit_price, exit_reason, duration_min,
            )

            if not dry_run:
                try:
                    await db.close_trade(
                        trade_db_id=t["id"],
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        pnl=pnl,
                        outcome=outcome,
                        duration_minutes=duration_min,
                        account_balance=close_bal,
                        account_equity=close_bal,
                    )
                except Exception:
                    logger.exception("close_trade failed for db_id=%s", t["id"])
                    totals["errors"] += 1

    logger.info("Reconciler done: %s", totals)
    await pool.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Reconcile stale DB-open ml_trades against cTrader.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report only; do not update ml_trades.")
    args = ap.parse_args()
    asyncio.run(reconcile(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
