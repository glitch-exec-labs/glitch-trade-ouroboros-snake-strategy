"""
ML Collector v2 — multi-timeframe, multi-model, PostgreSQL-backed.

Runs 6 bot groups (Hydra/M1, Viper/M5, Mamba/M15, Taipan/M30, Cobra/H1,
Anaconda/H4) staggered 10 seconds apart within a 60-second loop. Each bot
evaluates all configured symbols at its timeframe with its assigned model,
stores bars + signals in PostgreSQL, and optionally places demo trades.

Safety:
  - PID lock prevents duplicate instances
  - live=False hardcoded for CTraderClient
  - Account ID != production live account (asserted in config)
  - Market-hours gate blocks trade execution on weekends
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal as os_signal
import sys
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import BotConfig, Config, configure_logging, get_config
from .bar_fetcher import BarFetcher
from .db import DatabaseWriter, create_pool
from .strategy_runner import StrategyRunner

logger = logging.getLogger("ml_collector.collector")

_DEFAULT_SL_ATR_MULT = 1.5
_DEFAULT_TP_ATR_MULT = 2.0


# ─── Singleton lock ──────────────────────────────────────────────────────

class PidLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another ml_collector is already running (pid file: {self.path})")
        self._fh.truncate(0)
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# ─── Market-hours gate ──────────────────────────────────────────────────

def market_is_open(now: datetime) -> bool:
    wd = now.weekday()
    t = now.time()
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday before 22:00
        return t >= dtime(22, 0)
    if wd == 4 and t >= dtime(21, 0):  # Friday after 21:00
        return False
    return True


# ─── SL/TP derivation ──────────────────────────────────────────────────

def _derive_sl_tp(bar_close: float, indicators: dict, side: str):
    atr = float(indicators.get("atr", 0) or 0)
    if atr <= 0:
        atr = max(bar_close * 0.003, 1e-6)
    if side == "BUY":
        return bar_close - atr * _DEFAULT_SL_ATR_MULT, bar_close + atr * _DEFAULT_TP_ATR_MULT
    return bar_close + atr * _DEFAULT_SL_ATR_MULT, bar_close - atr * _DEFAULT_TP_ATR_MULT


# ─── Main ───────────────────────────────────────────────────────────────

async def run(cfg: Config) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from .order_placer import SymbolSpecCache, place_market_order
    from .sizer import BalanceCache, compute_adaptive_lots, rolling_win_rate, streak_multiplier
    from executor.ctrader_client import CTraderClient

    # Database pool
    pool = await create_pool(cfg.db_dsn)
    db = DatabaseWriter(pool)

    # Strategy runner (loads models lazily)
    runner = StrategyRunner()

    # Bar fetcher (patches TF map for M1/M5/M30)
    from ensemble.ctrader_price_feed import CTraderPriceFeed
    feed = CTraderPriceFeed()
    fetcher = BarFetcher(feed)

    # Per-account CTraderClients
    unique_accounts = {b.account_id for b in cfg.bots}
    clients: Dict[int, CTraderClient] = {
        aid: CTraderClient(
            client_id=cfg.ctrader_client_id,
            client_secret=cfg.ctrader_client_secret,
            access_token=cfg.ctrader_access_token,
            account_id=aid,
            live=False,
        )
        for aid in unique_accounts
    }
    spec_cache = SymbolSpecCache()
    balance_cache = BalanceCache(ttl_seconds=60.0)
    # Shared cache of the most-recent bar_close per symbol. Written by
    # evaluate_symbol on every fetch; read by monitor_loop (for closure
    # classification) and by _execute_trade (for JPY→USD conversion).
    latest_close_by_symbol: Dict[str, float] = {}

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGTERM, os_signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # ── Bot loop (one per timeframe group, staggered) ──────────────────

    async def bot_loop(bot: BotConfig, offset: int) -> None:
        if offset > 0:
            await asyncio.sleep(offset)

        while not stop.is_set():
            t0 = time.time()
            for symbol in cfg.symbols:
                if stop.is_set():
                    break
                try:
                    await evaluate_symbol(bot, symbol)
                except Exception:
                    logger.exception("%s/%s evaluation failed", bot.name, symbol)

            elapsed = time.time() - t0
            remaining = max(1.0, cfg.loop_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def evaluate_symbol(bot: BotConfig, symbol: str) -> None:
        # 1. Fetch bars (offloaded to thread pool since feed is sync)
        bars = await loop.run_in_executor(
            None, fetcher.fetch, symbol, bot.timeframe, bot.bar_count,
        )
        if bars is None:
            return

        # 2. Store bars in DB
        await db.insert_bars(symbol, bot.timeframe, bars)

        # 3. Check if this is a new bar
        if not fetcher.is_new_bar(bot.name, symbol, bars):
            return

        bar_time = fetcher.bar_time_utc(bars)
        bar_open = float(bars[-1, 1])
        bar_high = float(bars[-1, 2])
        bar_low = float(bars[-1, 3])
        bar_close = float(bars[-1, 4])
        latest_close_by_symbol[symbol] = bar_close

        # 4. Run model
        result = await loop.run_in_executor(
            None, runner.evaluate, bot, symbol, bars,
        )
        if result is None:
            return

        vote = str(result.get("vote", "HOLD")).upper()
        if vote not in ("BUY", "SELL", "HOLD"):
            vote = "HOLD"
        confidence = float(result.get("confidence", 0) or 0)
        reasoning = str(result.get("reasoning", ""))
        indicators = result.get("indicators", {}) or {}

        # 5. Store signal in DB
        executed = False
        signal_id = await db.insert_signal(
            bot_name=bot.name,
            model_name=bot.model,
            symbol=symbol,
            timeframe=bot.timeframe,
            account_id=bot.account_id,
            vote=vote,
            confidence=confidence,
            reasoning=reasoning,
            bar_time=bar_time,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            indicators=indicators,
            executed=False,
        )
        if signal_id is None:
            return  # deduped

        # 6. Maybe trade
        if vote in ("BUY", "SELL") and confidence >= bot.min_confidence:
            now = datetime.now(timezone.utc)
            if not market_is_open(now):
                logger.debug("%s/%s would trade %s but market closed", bot.name, symbol, vote)
            elif await db.has_open_trade(bot.name, symbol):
                logger.debug("%s/%s already has open trade, skipping", bot.name, symbol)
            else:
                await _execute_trade(pool, balance_cache, 
                    clients[bot.account_id], spec_cache, db,
                    bot, symbol, signal_id, vote, bar_close, confidence, indicators,
                )

        logger.info(
            "%s %s/%s %s conf=%.2f (bar=%s)",
            bot.name, symbol, bot.timeframe, vote, confidence,
            bar_time.strftime("%H:%M") if bar_time else "?",
        )

    async def _execute_trade(
        pool, balance_cache: "BalanceCache",
        client, spec_cache, db: DatabaseWriter,
        bot: BotConfig, symbol: str, signal_id: str,
        vote: str, bar_close: float, confidence: float, indicators: dict,
    ) -> None:
        side = vote.lower()
        sl, tp = _derive_sl_tp(bar_close, indicators, vote)

        spec = await spec_cache.get(client, bot.account_id, symbol)
        if spec is None:
            logger.warning("No symbol spec for %s@%d, skipping trade", symbol, bot.account_id)
            return

        # Adaptive sizing: live balance × notional_pct × streak_multiplier
        if bot.notional_pct and bot.notional_pct > 0:
            balance = await balance_cache.get(client, bot.account_id)
            win_rate, n_samples = await rolling_win_rate(pool, bot.name, window=10)
            mult = streak_multiplier(win_rate, n_samples)
            # JPY-quoted symbols need USDJPY conversion so the sizer produces
            # USD-equivalent exposure instead of JPY-denominated volumes.
            fx_rate = 1.0
            if symbol.upper().endswith("JPY") or symbol.upper() == "JPN225":
                fx_rate = latest_close_by_symbol.get("USDJPY", 150.0) or 150.0
            lots_calc, wire_vol = compute_adaptive_lots(
                balance=balance, notional_pct=bot.notional_pct,
                streak_mult=mult, price=bar_close, spec=spec,
                fx_rate_to_usd=fx_rate,
            )
            logger.info(
                "sizer %s %s: balance=$%.0f pct=%.3f win=%d/%d mult=%.2f fx=%.3f -> lots=%.3f wire=%d",
                bot.name, symbol, balance, bot.notional_pct, int(win_rate*n_samples), n_samples,
                mult, fx_rate, lots_calc, wire_vol,
            )
            result = await place_market_order(
                client=client, spec=spec, account_id=bot.account_id,
                symbol=symbol, side=side, lots=lots_calc, sl=sl, tp=tp,
                entry_price=bar_close, wire_volume=wire_vol,
            )
        else:
            result = await place_market_order(
                client=client, spec=spec, account_id=bot.account_id,
                symbol=symbol, side=side, lots=bot.lots, sl=sl, tp=tp,
                entry_price=bar_close,
            )

        if not result or not result.get("success"):
            logger.warning(
                "%s/%s trade failed: %s", bot.name, symbol, (result or {}).get("error"),
            )
            return

        pos_id = result.get("position_id") or result.get("order_id")
        if not pos_id:
            return

        entry_price = float(result.get("price") or bar_close)
        trade_db_id = await db.insert_trade(
            signal_id=signal_id,
            bot_name=bot.name,
            model_name=bot.model,
            symbol=symbol,
            timeframe=bot.timeframe,
            account_id=bot.account_id,
            side=vote,
            entry_price=entry_price,
            sl_price=sl,
            tp_price=tp,
            volume_lots=bot.lots,
            wire_volume=result.get("wire_volume", 0),
            ticket=str(pos_id),
            signal_confidence=confidence,
            signal_indicators=indicators,
        )
        if trade_db_id:
            await db.mark_signal_executed(signal_id, trade_db_id)
            logger.info(
                "TRADE %s %s %s entry=%.5f sl=%.5f tp=%.5f ticket=%s",
                bot.name, vote, symbol, entry_price, sl, tp, pos_id,
            )

    # ── Monitor loop (position closure detection) ──────────────────────

    async def monitor_loop() -> None:
        while not stop.is_set():
            try:
                open_trades = await db.get_open_trades()
                if open_trades:
                    # Group trades by account for efficient polling
                    by_account: Dict[int, List[dict]] = {}
                    for t in open_trades:
                        by_account.setdefault(t["account_id"], []).append(t)

                    for account_id, trades in by_account.items():
                        client = clients.get(account_id)
                        if not client:
                            continue
                        try:
                            positions = await client.get_open_positions()
                            live_tickets = {str(p.get("ticket")) for p in positions}

                            bal = await client.get_balance()
                            balance = float(bal.get("total", 0) or 0)
                            equity = float(bal.get("equity", balance) or balance)

                            for t in trades:
                                if t["ticket"] and t["ticket"] not in live_tickets:
                                    # Position closed — use the most recent bar_close
                                    # cached from the main eval loop. BarFetcher
                                    # returns None for <50 bars, so we can't just
                                    # ask for 2 bars on demand here.
                                    current = latest_close_by_symbol.get(t["symbol"])
                                    if current is None:
                                        # Fallback: fetch a full set of bars on miss.
                                        try:
                                            c = fetcher.fetch(t["symbol"], "m15", 100)
                                            if c is not None and len(c) > 0:
                                                current = float(c[-1, 4])
                                        except Exception:
                                            pass

                                    exit_price, exit_reason, outcome = _classify_closure(t, current)
                                    direction = 1 if t["side"] == "BUY" else -1
                                    pnl = (exit_price - t["entry_price"]) * direction * t["volume_lots"] * 100.0 if exit_price else 0
                                    duration = (datetime.now(timezone.utc) - t["opened_at"]).total_seconds() / 60.0

                                    await db.close_trade(
                                        trade_db_id=t["id"],
                                        exit_price=exit_price or 0,
                                        exit_reason=exit_reason,
                                        pnl=pnl,
                                        outcome=outcome,
                                        duration_minutes=duration,
                                        account_balance=balance,
                                        account_equity=equity,
                                    )
                                    logger.info(
                                        "CLOSURE %s %s %s outcome=%s pnl=%.2f",
                                        t["bot_name"], t["symbol"], exit_reason, outcome, pnl,
                                    )
                        except Exception:
                            logger.exception("monitor_loop failed for account %s", account_id)
            except Exception:
                logger.exception("monitor_loop iteration failed")

            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.position_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # ── Start ──────────────────────────────────────────────────────────

    logger.info(
        "ML collector v2 starting — db=%s symbols=%d bots=%s",
        cfg.db_dsn.split("@")[-1],
        len(cfg.symbols),
        [(b.name, b.model, b.timeframe) for b in cfg.bots],
    )

    tasks = []
    for i, bot in enumerate(cfg.bots):
        tasks.append(bot_loop(bot, offset=i * 10))
    tasks.append(monitor_loop())

    await asyncio.gather(*tasks)
    await pool.close()
    logger.info("Collector stopped cleanly")


def _classify_closure(trade: dict, current: Optional[float]):
    if current is None or current <= 0:
        return 0.0, "manual_or_unknown", "UNKNOWN"
    side = trade["side"]
    sl = trade.get("sl_price")
    tp = trade.get("tp_price")
    entry = trade.get("entry_price", 0)
    if side == "BUY":
        if tp and current >= tp:
            return current, "tp_hit", "WIN"
        if sl and current <= sl:
            return current, "sl_hit", "LOSS"
        return current, "manual_or_unknown", "WIN" if current > entry else "LOSS"
    else:
        if tp and current <= tp:
            return current, "tp_hit", "WIN"
        if sl and current >= sl:
            return current, "sl_hit", "LOSS"
        return current, "manual_or_unknown", "WIN" if current < entry else "LOSS"


def main() -> int:
    cfg = get_config()
    configure_logging(cfg)

    lock = PidLock(cfg.state_dir / "collector.pid")
    try:
        lock.acquire()
    except RuntimeError as e:
        logger.error(str(e))
        return 1

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception:
        logger.exception("Collector crashed")
        return 2
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
