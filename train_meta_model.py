"""Phase 2: train a per-strategy meta-model that scores a BUY signal's
indicator features with a confidence (probability of hitting take-profit
before stop-loss/timing out), using the triple-barrier labels from Phase 1
(signal_logger.py + label_signals.py).

Trains only on each strategy's clean dataset (id > its CLEAN_DATA_WATERMARK)
— see label_signals.py / run_multi_backtest.py history: earlier signals
were duplicated ~6-21x by a sleeptime bug and are excluded to avoid training
on non-independent repeated rows. mean_reversion/vwap have a SEPARATE, later
watermark (62279) because their entry logic was fully replaced on 2026-08-30
(see regime_indicators.py / project memory) — data from before that point
reflects a different, now-deleted strategy and would corrupt training if
mixed in with the new logic's signals.

Time-ordered train/test split per strategy (last 20% of dates held out) so
evaluation reflects forecasting forward in time, not shuffled leakage.

Usage:
    python train_meta_model.py
"""

import json
import logging

import joblib
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, roc_auc_score

from signal_logger import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default watermark: signals with id <= this were logged before the
# sleeptime/backtest fix and are duplicated ~6-21x per real trading day (see
# project memory). Per-strategy overrides below take precedence.
CLEAN_DATA_WATERMARK = 46380
CLEAN_DATA_WATERMARK_OVERRIDES = {
    # Entry logic fully replaced 2026-08-30 — see project memory
    # "project-live-mean-reversion-vwap-replacement".
    "mean_reversion": 62279,
    # vwap replaced AGAIN 2026-08-30 (same day) with VWAP+Breakout — a
    # second, different entry logic replacing the first replacement. All
    # prior vwap rows (including the first replacement's) were deleted from
    # signals.db, so this watermark is really just documentation now, but
    # keep it current in case old rows ever get restored from a backup.
    "vwap": 66149,
    # reversal replaced 2026-08-30 with a gap-fade entry. All prior reversal
    # rows (5-day-monotonic-decline logic) were deleted from signals.db.
    "reversal": 76473,
}

MODELS_DIR = "models"

# Feature keys that are numeric/boolean and meaningful across strategies.
# Not every strategy populates every key (e.g. momentum_score only exists for
# momentum) — missing keys are filled with 0.0 per-row.
FEATURE_KEYS = [
    "trend_diff_pct",
    "volume_ratio",
    "volume_confirmed",
    "direction_confirmed",
    "momentum_score",
    "close_position_in_range",
]

MIN_SAMPLES_PER_STRATEGY = 30


def _load_labeled_buys() -> pd.DataFrame:
    watermark_case = " ".join(
        f"WHEN '{name}' THEN {wm}" for name, wm in CLEAN_DATA_WATERMARK_OVERRIDES.items()
    )
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT strategy_name, symbol, timestamp, confirmed, outcome_label, features
            FROM (
                SELECT *, MIN(id) OVER (
                    PARTITION BY strategy_name, symbol, timestamp, confirmed, entry_price
                ) AS keep_id, id
                FROM signals
                WHERE action = 'BUY' AND outcome_label IS NOT NULL
                  AND id > (CASE strategy_name {watermark_case} ELSE ? END)
            )
            WHERE id = keep_id
            """,
            (CLEAN_DATA_WATERMARK,),
        ).fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        features = json.loads(row["features"]) if row["features"] else {}
        record = {
            "strategy_name": row["strategy_name"],
            "symbol": row["symbol"],
            "timestamp": row["timestamp"],
            "confirmed": int(row["confirmed"] or 0),
            "label": int(row["outcome_label"]),
        }
        for key in FEATURE_KEYS:
            value = features.get(key)
            if isinstance(value, bool):
                value = int(value)
            record[key] = float(value) if value is not None else 0.0
        records.append(record)

    return pd.DataFrame.from_records(records)


def _time_ordered_split(df: pd.DataFrame, test_frac: float = 0.2):
    df = df.sort_values("timestamp")
    split_idx = int(len(df) * (1 - test_frac))
    return df.iloc[:split_idx], df.iloc[split_idx:]


def train_all():
    df = _load_labeled_buys()
    logger.info(
        f"Loaded {len(df)} deduplicated, labeled BUY signals "
        f"(default watermark id > {CLEAN_DATA_WATERMARK}; overrides: {CLEAN_DATA_WATERMARK_OVERRIDES})"
    )

    feature_cols = FEATURE_KEYS + ["confirmed"]
    results = {}

    for strategy_name, group in df.groupby("strategy_name"):
        n = len(group)
        baseline_win_rate = group["label"].mean()

        if n < MIN_SAMPLES_PER_STRATEGY:
            logger.warning(
                f"{strategy_name}: only {n} labeled samples (< {MIN_SAMPLES_PER_STRATEGY}), "
                f"skipping — not enough data yet for a meta-model."
            )
            results[strategy_name] = {"status": "skipped", "n": n}
            continue

        train_df, test_df = _time_ordered_split(group)
        if train_df["label"].nunique() < 2:
            logger.warning(f"{strategy_name}: training split has only one class, skipping.")
            results[strategy_name] = {"status": "skipped_single_class", "n": n}
            continue

        model = LGBMClassifier(
            n_estimators=50,
            max_depth=3,
            num_leaves=7,
            min_child_samples=5,
            verbosity=-1,
        )
        model.fit(train_df[feature_cols], train_df["label"])

        test_acc = None
        test_auc = None
        if len(test_df) > 0:
            preds = model.predict(test_df[feature_cols])
            test_acc = accuracy_score(test_df["label"], preds)
            if test_df["label"].nunique() == 2:
                proba = model.predict_proba(test_df[feature_cols])[:, 1]
                test_auc = roc_auc_score(test_df["label"], proba)

        joblib.dump({"model": model, "features": feature_cols}, f"{MODELS_DIR}/{strategy_name}.pkl")

        results[strategy_name] = {
            "status": "trained",
            "n": n,
            "n_train": len(train_df),
            "n_test": len(test_df),
            "baseline_win_rate": round(baseline_win_rate, 3),
            "test_accuracy": round(test_acc, 3) if test_acc is not None else None,
            "test_auc": round(test_auc, 3) if test_auc is not None else None,
        }
        logger.info(f"{strategy_name}: {results[strategy_name]}")

    return results


if __name__ == "__main__":
    train_all()
