"""Secondary-indicator confirmation for strategy signals.

When a strategy fires a BUY, this module runs one extra, independent check
before treating the signal as "confirmed" — reducing false positives from
any single strategy's narrow rule set.

Design choice: only BUY (entry) signals are gated by confirmation. SELL
(exit) signals always execute — blocking an exit on a secondary indicator's
disagreement would mean holding a losing position longer than the primary
strategy's own risk logic intended.

Design choice: confirmation logic is strategy-type-aware. Types:
  - "trend": strategy buys breakouts/continuations — confirm price is
    above its longer-term trend (momentum, scalping, breakout, gap_and_go).
    Sentiment isn't its own strategy (2026-09-01) — it's a symbol-selection
    input for scalping.py, so it never calls confirm_signal directly.
  - "self_gated": the strategy's own analyze() already applies an
    equivalent secondary-style filter internally (mean_reversion, vwap,
    reversal — see their module docstrings and regime_indicators.py). A
    second, differently-tuned confirmation layer on top would be redundant
    at best, and at worst reapply stale tuning: this type used to be
    "reversion" (mean_reversion), "reversion_inverse" (vwap, inverted
    because the *old* vwap entry logic made that check anti-predictive),
    and "capitulation" (reversal, tuned against the *old* 5-day-monotonic-
    decline trigger) — none of that tuning carries over once the entry
    logic itself changed. So this type always confirms, while still
    computing and logging the same trend-style features for meta-model
    training continuity.
"""

import logging
from typing import Tuple, Dict

from asset_class_riders import check_vix_rider

logger = logging.getLogger(__name__)

# Today's volume must be at least this multiple of its 20-day average
VOLUME_RATIO_THRESHOLD = 1.2

# Longer-term moving average window used as the trend filter
TREND_LOOKBACK_DAYS = 50

# Classification of each strategy's BUY intent, used to select the correct
# confirmation rule. Update this mapping if new strategies are added.
STRATEGY_TYPES = {
    "momentum": "trend",
    "scalping": "trend",
    "breakout": "trend",
    "gap_and_go": "trend",
    "mean_reversion": "self_gated",
    "vwap": "self_gated",
    "reversal": "self_gated",
    # market_profile (2026-09-02): buys a Value Area breakout, i.e. price
    # accepted above resistance-like structure — the same "confirm the
    # trend is real" intent as breakout/gap_and_go, not a self-gated
    # regime-classified entry like mean_reversion/vwap/reversal.
    "market_profile": "trend",
    # scalping_v2 (2026-09-08): its own entry condition already requires a
    # Stoch RSI cross confirming a MACD histogram flip — that IS the
    # secondary-indicator confirmation this module exists to provide, on
    # intraday minute data. Stacking "trend"'s daily-bar 50-day-SMA/volume
    # gate on top would reproduce the same near-zero confirm rate already
    # confirmed for scalping.py (~2.2%, tuned for daily-bar strategies, not
    # minute-bar ones) for a different reason. Same category as
    # mean_reversion/vwap/reversal: self-gated, always confirms here.
    "scalping_v2": "self_gated",
}


def confirm_signal(strategy, symbol: str, action: str, strategy_name: str = None) -> Tuple[bool, Dict]:
    """Run a secondary-indicator check on a primary signal.

    Args:
        strategy: the running Strategy instance (gives access to
                  get_historical_prices)
        symbol: stock symbol
        action: "BUY" or "SELL"
        strategy_name: which strategy generated this signal (e.g. "breakout",
                        "mean_reversion") — determines which confirmation
                        rule applies. Defaults to "trend" behavior if unknown.

    Returns:
        (confirmed: bool, features: dict) — features are logged regardless
        of the confirmation outcome, for later meta-model training.
    """
    strategy_type = STRATEGY_TYPES.get(strategy_name, "trend")
    features = {"strategy_type": strategy_type}

    # Optional VIX "second opinion" (2026-09-05, see asset_class_riders.py)
    # — applies uniformly regardless of strategy_type since all 8 live
    # strategies trade individual US equities only. Only actually blocks a
    # BUY when it fetched real data and genuinely disagrees; fails open
    # (vix_confirmed=True) on any data problem, same as this module's own
    # confirmation_error handling below.
    vix_confirmed, vix_features = check_vix_rider()
    features.update(vix_features)

    try:
        bars = strategy.get_historical_prices(symbol, TREND_LOOKBACK_DAYS, "day")
        if not bars or len(bars.df) < TREND_LOOKBACK_DAYS:
            features["confirmation_error"] = "insufficient_history"
            return False, features

        df = bars.df
        current_price = float(df["close"].iloc[-1])
        current_volume = float(df["volume"].iloc[-1])
        avg_volume = float(df["volume"].iloc[-20:].mean())
        sma_long = float(df["close"].iloc[-TREND_LOOKBACK_DAYS:].mean())

        volume_ratio = (current_volume / avg_volume) if avg_volume else 0.0
        trend_diff_pct = ((current_price - sma_long) / sma_long) if sma_long else 0.0

        features.update({
            "volume_ratio": round(volume_ratio, 4),
            "trend_diff_pct": round(trend_diff_pct, 4),
            "current_price": round(current_price, 4),
            "sma_long": round(sma_long, 4),
        })

        if strategy_type == "trend":
            # Trend-following: confirm price is on the correct side of its
            # longer-term average — i.e. actually in an uptrend, not just
            # a brief spike against the broader trend.
            volume_confirmed = volume_ratio >= VOLUME_RATIO_THRESHOLD
            direction_confirmed = current_price >= sma_long
            features["direction_check"] = "price_above_sma_long"

            confirmed = volume_confirmed and direction_confirmed and vix_confirmed
            features["volume_confirmed"] = volume_confirmed
            features["direction_confirmed"] = direction_confirmed
            return confirmed, features

        elif strategy_type == "self_gated":
            # mean_reversion.py / vwap.py already gate their own entries on
            # regime (ADX), volatility (ATR vs. its own median), and volume
            # spikes internally — see regime_indicators.py. Always confirm
            # on this module's own logic; still subject to the VIX rider
            # above like every other strategy. Still compute the same
            # trend-style features as "trend" below so meta-model training
            # has a consistent feature set.
            volume_confirmed = volume_ratio >= VOLUME_RATIO_THRESHOLD
            direction_confirmed = current_price >= sma_long
            features["direction_check"] = "self_gated_no_secondary_filter"

            features["volume_confirmed"] = volume_confirmed
            features["direction_confirmed"] = direction_confirmed
            return vix_confirmed, features

        else:
            # Unknown strategy type — default to the trend-following rule.
            volume_confirmed = volume_ratio >= VOLUME_RATIO_THRESHOLD
            direction_confirmed = current_price >= sma_long
            features["direction_check"] = "price_above_sma_long_default"

            confirmed = volume_confirmed and direction_confirmed and vix_confirmed
            features["volume_confirmed"] = volume_confirmed
            features["direction_confirmed"] = direction_confirmed
            return confirmed, features

    except Exception as e:
        logger.error(f"Error confirming signal for {symbol}: {e}")
        features["confirmation_error"] = str(e)
        return False, features
