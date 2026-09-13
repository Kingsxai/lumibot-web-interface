"""Phase 2: load a strategy's trained meta-model (see train_meta_model.py)
and score a BUY signal's features with a confidence — the model's estimated
probability of hitting take-profit before stop-loss/timing out.

Not wired into live/paper trading yet — that's Phase 4 (a MIN_SIGNAL_CONFIDENCE
gate in strategy_manager.py). This module only exposes the scoring function
Phase 4 will call.

Usage:
    from meta_model import get_confidence
    confidence = get_confidence("breakout", confirm_features, confirmed=True)
"""

import logging
import os

import joblib
import pandas as pd

logger = logging.getLogger(__name__)

MODELS_DIR = "models"

_model_cache = {}


def _load_model(strategy_name: str):
    if strategy_name in _model_cache:
        return _model_cache[strategy_name]

    path = os.path.join(MODELS_DIR, f"{strategy_name}.pkl")
    if not os.path.exists(path):
        _model_cache[strategy_name] = None
        return None

    bundle = joblib.load(path)
    _model_cache[strategy_name] = bundle
    return bundle


def get_confidence(strategy_name: str, features: dict, confirmed: bool) -> float | None:
    """Return the meta-model's confidence (0-1) that this BUY signal will hit
    take-profit before stop-loss/timing out, or None if no model exists yet
    for this strategy (too little labeled data — see train_meta_model.py).
    """
    bundle = _load_model(strategy_name)
    if bundle is None:
        return None

    model = bundle["model"]
    feature_cols = bundle["features"]

    row = {}
    for col in feature_cols:
        if col == "confirmed":
            row[col] = int(confirmed)
            continue
        value = features.get(col)
        if isinstance(value, bool):
            value = int(value)
        row[col] = float(value) if value is not None else 0.0

    ordered = pd.DataFrame([[row[col] for col in feature_cols]], columns=feature_cols)
    return float(model.predict_proba(ordered)[0][1])
