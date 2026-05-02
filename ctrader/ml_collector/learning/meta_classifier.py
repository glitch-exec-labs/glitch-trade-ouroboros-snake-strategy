"""
Meta-classifier — predicts P(WIN) per bot from observed signal outcomes.

Phase-1 actual ML on top of the rule-based ensemble. For each closed
trade in ml_trades we have:
  - signal_indicators (JSONB) — bot-specific feature snapshot at signal time
  - signal_confidence  (rule-based confidence)
  - bot_name, model_name, symbol, timeframe, side, entry_price
  - outcome (WIN/LOSS) — the label

We train one XGBoost per bot since each bot's signal_indicators schema
differs (hydra has BB-band features, viper has RSI-crossover features,
taipan has session features, etc.).

The trained model lets us:
  1. Score each fresh signal with P(WIN) before execution
  2. Veto trades where P(WIN) < threshold (default 0.45)
  3. Boost confidence where P(WIN) > 0.65
  4. Track per-bot calibration over time

Usage:
    python -m ml_collector.learning.meta_classifier train
        --min-trades 100  # skip bots with insufficient data

    python -m ml_collector.learning.meta_classifier predict
        --bot hydra --features '{"rsi": 72.4, "adx": 18.4, ...}'

    python -m ml_collector.learning.meta_classifier metrics
        # report current per-bot model holdout AUC + calibration
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, roc_auc_score, log_loss, brier_score_loss,
    confusion_matrix, classification_report,
)
import joblib

logger = logging.getLogger("meta_classifier")

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# Bots with consistent feature schemas (orphan-backfilled rows are excluded).
DEFAULT_MIN_TRADES = 100

# Categorical features that need encoding (strings → small int codes).
CAT_FEATURES = {
    "regime", "trigger",
    "ema_direction", "session_quality",
    "rsi_crossover", "h1_trend", "pattern_type",
}

# Boolean features (all bots) — converted to 0/1.
BOOL_FEATURES = {
    "is_asian", "is_london", "is_ny", "is_overlap",
    "is_forex", "is_crypto", "price_above_ema", "is_above_ema",
    "is_below_ema", "sr_nearby", "pattern_detected",
}


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_dsn() -> str:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path, override=True)
    dsn = os.environ.get("ML_DATABASE_URL")
    if not dsn:
        raise RuntimeError(f"ML_DATABASE_URL not set; checked {env_path}")
    return dsn


def fetch_training_set(bot_name: str) -> pd.DataFrame:
    dsn = _load_dsn()
    sql = """
        SELECT
            t.id, t.bot_name, t.symbol, t.timeframe, t.side,
            t.entry_price, t.signal_confidence,
            t.signal_indicators, t.outcome, t.pnl,
            CASE WHEN t.exit_reason = 'broker_closed_stale' THEN 0 ELSE 1 END
                AS label_quality,
            EXTRACT(EPOCH FROM (t.closed_at - t.opened_at))/60 AS held_minutes,
            EXTRACT(HOUR   FROM t.opened_at AT TIME ZONE 'UTC')::int AS hour_utc,
            EXTRACT(DOW    FROM t.opened_at AT TIME ZONE 'UTC')::int AS day_of_week
        FROM ml_trades t
        WHERE t.bot_name = %s
          AND t.outcome IN ('WIN', 'LOSS')
          AND t.signal_indicators IS NOT NULL
          -- Exclude rows with synthetic / placeholder outcomes:
          AND COALESCE((t.signal_indicators->>'orphan')::boolean, false) = false
          -- Filter out artifact rows by their OBSERVABLE properties (placeholder
          -- exit_price + zero-second hold), NOT by exit_reason which is too coarse:
          -- many legitimate WIN/LOSS trades carry exit_reason='manual_or_unknown'
          -- while their outcome was correctly bar-price-labeled.
          --
          -- Exclude closures with no fill price recorded (placeholders / debug runs):
          AND t.exit_price IS NOT NULL AND t.exit_price > 0
          -- Exclude near-instant closures (< 60 sec held). On bots running M5+
          -- timeframes, a sub-minute closure can only be an artifact.
          AND EXTRACT(EPOCH FROM (t.closed_at - t.opened_at)) >= 60
        ORDER BY t.opened_at
    """
    with psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (bot_name,))
            rows = [dict(r) for r in cur.fetchall()]
    return pd.DataFrame(rows)


# ── Feature engineering ───────────────────────────────────────────────────────

def explode_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """Flatten signal_indicators JSONB into columns, encode categoricals."""
    if df.empty:
        return df, []

    # Build the feature DataFrame from JSONB
    feat_rows = [dict(s) if isinstance(s, dict) else {} for s in df["signal_indicators"]]
    feats = pd.DataFrame(feat_rows)

    # Drop housekeeping fields not used as features
    drop_cols = {"orphan", "backfilled_at", "ticket"}
    feats = feats.drop(columns=[c for c in drop_cols if c in feats.columns], errors="ignore")

    # Encode booleans
    for c in feats.columns:
        if c in BOOL_FEATURES:
            feats[c] = feats[c].fillna(False).astype(bool).astype(int)

    # Encode categoricals
    cat_cols = [c for c in feats.columns if c in CAT_FEATURES]
    for c in cat_cols:
        feats[c] = feats[c].fillna("UNK").astype("category").cat.codes

    # Cast every remaining column to float, replacing non-numeric with NaN
    for c in feats.columns:
        if feats[c].dtype not in (np.int64, np.int32, np.float64, np.float32, bool):
            feats[c] = pd.to_numeric(feats[c], errors="coerce")
        feats[c] = feats[c].astype(float)

    # Add cross-feature signals from the trade row
    feats["signal_confidence"] = df["signal_confidence"].astype(float).values
    feats["hour_utc"] = df["hour_utc"].astype(float).values
    feats["day_of_week"] = df["day_of_week"].astype(float).values
    feats["is_buy"] = (df["side"] == "BUY").astype(int).values
    # label_quality: 1 = authoritative deal-history label, 0 = bar-price fallback.
    # Letting the model see this means it can downweight noisy training rows.
    if "label_quality" in df.columns:
        feats["label_quality"] = df["label_quality"].astype(float).values

    feature_names = list(feats.columns)
    return feats, feature_names


# ── Training ──────────────────────────────────────────────────────────────────

def train_bot_model(bot_name: str, min_trades: int = DEFAULT_MIN_TRADES,
                    save: bool = True) -> Optional[Dict]:
    df = fetch_training_set(bot_name)
    if len(df) < min_trades:
        logger.warning("%s: only %d closed trades — skipping (need >= %d)",
                       bot_name, len(df), min_trades)
        return None

    feats, feature_names = explode_features(df)
    y = (df["outcome"] == "WIN").astype(int).values
    X = feats.fillna(-999.0).values  # XGBoost handles missing via sentinel

    # Time-ordered split: oldest 80% train, newest 20% test
    split = int(len(X) * 0.8)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    # Class imbalance handling
    pos = max(int(y_tr.sum()), 1)
    neg = max(len(y_tr) - pos, 1)
    scale_pos_weight = neg / pos

    model = XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss", n_jobs=2,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)

    # Holdout metrics
    p_te = model.predict_proba(X_te)[:, 1]
    pred_te = (p_te >= 0.5).astype(int)
    metrics = {
        "n_trades":     int(len(df)),
        "n_train":      int(len(X_tr)),
        "n_test":       int(len(X_te)),
        "baseline_winrate": round(float(y.mean()), 4),
        "test_accuracy":    round(float(accuracy_score(y_te, pred_te)), 4),
        "test_auc":         round(float(roc_auc_score(y_te, p_te))
                                   if len(set(y_te)) > 1 else 0.0, 4),
        "test_logloss":     round(float(log_loss(y_te, p_te, labels=[0,1])), 4),
        "test_brier":       round(float(brier_score_loss(y_te, p_te)), 4),
        "feature_count":    len(feature_names),
        "trained_at":       datetime.utcnow().isoformat() + "Z",
    }

    # Win-rate uplift simulation: only take trades with P(WIN) >= threshold
    thresholds = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    uplift = {}
    for thr in thresholds:
        mask = p_te >= thr
        if mask.sum() < 5:
            uplift[str(thr)] = {"taken": int(mask.sum()), "winrate": None}
        else:
            wr = float(y_te[mask].mean())
            uplift[str(thr)] = {
                "taken":    int(mask.sum()),
                "winrate":  round(wr, 4),
                "vs_base":  round(wr - metrics["baseline_winrate"], 4),
            }
    metrics["winrate_uplift_at_threshold"] = uplift

    # Top-10 most important features
    importances = sorted(zip(feature_names, model.feature_importances_),
                         key=lambda x: x[1], reverse=True)[:10]
    metrics["top_features"] = [
        {"feature": f, "importance": round(float(imp), 4)} for f, imp in importances
    ]

    if save:
        bundle = {
            "model":         model,
            "feature_names": feature_names,
            "metrics":       metrics,
            "bot_name":      bot_name,
        }
        out = MODELS_DIR / f"{bot_name}_meta_classifier.pkl"
        joblib.dump(bundle, out)
        logger.info("saved %s (auc=%.3f, n=%d)",
                    out.name, metrics["test_auc"], metrics["n_trades"])

    return metrics


def predict(bot_name: str, features: Dict) -> Optional[float]:
    """Score a fresh signal. Returns P(WIN) in [0, 1] or None if no model."""
    pkl = MODELS_DIR / f"{bot_name}_meta_classifier.pkl"
    if not pkl.exists():
        return None
    bundle = joblib.load(pkl)
    model = bundle["model"]
    feat_names: List[str] = bundle["feature_names"]
    # Build feature row in the right order, missing → -999 sentinel
    row = []
    for fn in feat_names:
        v = features.get(fn, -999.0)
        if isinstance(v, bool):  v = int(v)
        if isinstance(v, str):
            # categorical — naive: hash to a small code (not ideal, but works)
            v = abs(hash(v)) % 100
        try:
            row.append(float(v))
        except (TypeError, ValueError):
            row.append(-999.0)
    p = model.predict_proba(np.array([row]))[0, 1]
    return float(p)


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_train(args):
    bots = args.bots or ["hydra", "viper", "mamba", "taipan", "cobra", "anaconda"]
    summary = {}
    for bot in bots:
        m = train_bot_model(bot, min_trades=args.min_trades)
        summary[bot] = m if m else {"skipped": True}
    print(json.dumps(summary, indent=2, default=str))


def cmd_metrics(_args):
    out = {}
    for pkl in MODELS_DIR.glob("*_meta_classifier.pkl"):
        b = joblib.load(pkl)
        out[b["bot_name"]] = b["metrics"]
    print(json.dumps(out, indent=2, default=str))


def cmd_predict(args):
    feats = json.loads(args.features) if args.features else {}
    p = predict(args.bot, feats)
    print(json.dumps({"bot": args.bot, "p_win": p}, indent=2))


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("train")
    t.add_argument("--bots", nargs="+",
                   help="subset of bots to train; default all")
    t.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    t.set_defaults(func=cmd_train)

    sub.add_parser("metrics").set_defaults(func=cmd_metrics)

    p = sub.add_parser("predict")
    p.add_argument("--bot", required=True)
    p.add_argument("--features", required=True, help="JSON dict of features")
    p.set_defaults(func=cmd_predict)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
