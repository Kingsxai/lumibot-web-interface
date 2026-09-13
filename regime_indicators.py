"""Shared regime-detection indicators for mean_reversion.py, vwap.py, and
reversal.py.

Bollinger Band / ADX / ATR / rolling-VWAP / gap thresholds validated
2026-08-30 in experimental_bb_momentum_hedge.py — a standalone sandbox test
across forex, index ETFs, large-cap stocks, commodities, growth stocks,
crypto, futures, biotech/pharma, and small-cap hype names. See that file and
project memory for the full evidence behind each strategy's specific
thresholds; nothing here is re-derived, only reused.

mean_reversion.py uses the BB reversion helpers (reversion_regime_ok).
vwap.py (replaced 2026-08-30, second time same day) uses the VWAP+Breakout
trend-following helpers (breakout_confirmed) — a structurally different,
broader-winning strategy than the rolling-VWAP reversion it replaced.
reversal.py (replaced 2026-08-30) uses the gap-fade helper
(reversal_fade_confirmed) — bets a gap-down fails and price bounces, instead
of the original "5-day monotonic decline" trigger.
"""

import numpy as np
import pandas as pd

BB_WINDOW = 20
BB_NUM_STD = 2
ATR_WINDOW = 14
ADX_WINDOW = 14
ADX_RANGE_THRESHOLD = 20  # below this = range-bound, eligible for reversion entries
ATR_EXPANSION_MAX = 1.3   # skip entry if ATR > this x its own 50-day median (volatility expanding)
VOLUME_SPIKE_MAX = 1.5    # skip entry if volume > this x its 20-day average (news-driven day)
VOLUME_CONFIRM_MIN = 1.2  # vwap.py breakout requires volume >= this x its 20-day average
VWAP_WINDOW = 20
VWAP_DISCOUNT = 0.995     # unused by vwap.py now, kept for reference/other experiments

VWAP_BAND_STD = 1            # vwap.py breakout: "just beyond VWAP band (1 std deviation)"
VWAP_BREAKOUT_LOOKBACK = 10  # daily-bar "session high" proxy
VWAP_BREAKOUT_ATR_CAP = 0.5  # stop distance capped at this x ATR
VWAP_BREAKOUT_MIN_STOP_ATR = 0.1  # floor so a razor-thin band breakout doesn't oversize the position

GAP_THRESHOLD = 0.03            # matches gap_and_go.py's existing gap-up threshold
REVERSAL_FADE_VOLUME_MIN = 1.3  # capitulation-style volume spike
REVERSAL_FADE_CLOSE_POS_MIN = 0.6  # close in the upper 40% of the day's range = exhaustion/recovery
GAP_ATR_STOP_MULT = 1.5         # "ATR-based dynamic stops (1.5-2x ATR)" — using the tighter end
GAP_REWARD_R = 2                # reversal.py's validated exit: fixed 2R target off the ATR stop

# 2026-09-09, explicit user request: "keep the stop loss as it is but
# take profit change it to the minimum and let's see how does it feels
# on a live system" -- applies to mean_reversion.py/vwap.py/reversal.py,
# the three strategies with their own dynamic (ATR/SMA-derived) exit
# levels rather than the uniform percent-based bracket. Their STOP stays
# exactly as validated (2x ATR / VWAP-band-capped / 1.5x ATR — unchanged
# by this); only the TARGET is replaced with this tiny fixed percent
# above entry, decoupled from the stop distance entirely, so any small
# favorable move exits instead of waiting for the strategy's own
# original target (SMA20 reversion / 2R). Matches the smallest value
# actually backtested (see sandbox_reduced_takeprofit_test.py, run
# 2026-09-09 against momentum/breakout/gap_and_go's own uniform-percent
# exit — 0.02% and 0.1% produced identical results there, both far
# smaller than normal daily price noise). This is a deliberate, informed
# experiment per the user's own request -- going out live to observe
# real live behavior directly per explicit instruction, not backtested
# first for these three specifically (unlike momentum/breakout/
# gap_and_go's earlier test) -- if this needs revisiting, that's a real
# decision to make with the user, not something to silently backtest-
# and-revert on a future session's own initiative.
#
# 2026-09-13, explicit user decision after the experiment was measured:
# 0.02% produced 44 mean_reversion take_profit exits averaging a 0.064%
# move for a NET LOSS of -$30.93 (plus 3 reversal TP exits at exactly
# $0.00) -- the target sat inside the bid/ask spread + fill slippage, so
# a "winning" exit filled below entry on average. User's call: "raise to
# >=0.5%". 0.5% clears typical large-cap spread+slippage (~0.05-0.15%)
# by several multiples so a take_profit exit actually books a gain. The
# stops are still untouched (2x ATR / 1.5x ATR / VWAP-band-capped), so
# reward:risk stays well below 1:1 -- this is a live observation setting,
# not a validated exit; the strategies' own original targets (SMA20 /
# 2R) remain the backtest-validated ones if this is ever revisited.
MINIMAL_TAKE_PROFIT_PCT = 0.005

MIN_BARS_REQUIRED = 90    # enough history for the 50-day ATR median plus warmup


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds sma20/bb_upper/bb_lower/vwap20/atr/adx/atr_median50/volume_avg20
    columns to a daily OHLCV dataframe (columns: open/high/low/close/volume,
    lowercase, as returned by Lumibot's get_historical_prices().df)."""
    df = df.copy()
    # International symbols (ib_side_channel_trader.py) can hand back
    # volume as decimal.Decimal instead of float — confirmed live
    # 2026-09-02, crashed the whole international/forex cycle for every
    # symbol that pass (TypeError: unsupported operand type(s) for *:
    # 'float' and 'decimal.Decimal', since typical_price*volume mixes a
    # float Series with an object-dtype Decimal Series). Normalize the
    # numeric columns unconditionally — a no-op when they're already
    # float (the normal case for every domestic strategy), cheap
    # insurance against whatever type a given data source hands back.
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = df[col].astype(float)
    df["sma20"] = df["close"].rolling(BB_WINDOW).mean()
    std20 = df["close"].rolling(BB_WINDOW).std()
    df["bb_upper"] = df["sma20"] + BB_NUM_STD * std20
    df["bb_lower"] = df["sma20"] - BB_NUM_STD * std20

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap20"] = (
        (typical_price * df["volume"]).rolling(VWAP_WINDOW).sum()
        / df["volume"].rolling(VWAP_WINDOW).sum()
    )
    vwap_deviation_std = (df["close"] - df["vwap20"]).rolling(VWAP_WINDOW).std()
    df["vwap_upper"] = df["vwap20"] + VWAP_BAND_STD * vwap_deviation_std
    # Prior N-day high, excluding today — the "session high" breakout level.
    df["prior_high"] = df["high"].shift(1).rolling(VWAP_BREAKOUT_LOOKBACK).max()

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_WINDOW, adjust=False).mean()

    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_for_di = tr.ewm(alpha=1 / ADX_WINDOW, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / ADX_WINDOW, adjust=False).mean() / atr_for_di
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / ADX_WINDOW, adjust=False).mean() / atr_for_di
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["adx"] = dx.ewm(alpha=1 / ADX_WINDOW, adjust=False).mean()

    df["atr_median50"] = df["atr"].rolling(50).median()
    df["volume_avg20"] = df["volume"].rolling(20).mean()

    df["prev_close"] = prev_close
    df["gap_pct"] = (df["open"] - prev_close) / prev_close
    day_range = df["high"] - df["low"]
    df["close_position_in_range"] = ((df["close"] - df["low"]) / day_range).where(day_range > 0, 0.0)

    return df


def reversion_regime_ok(row) -> bool:
    """All three gates a mean-reversion-style entry (BB or VWAP) needs:
    range-bound (not trending), stable volatility (not breaking out), and
    no volume spike (not news-driven)."""
    range_bound = row["adx"] < ADX_RANGE_THRESHOLD
    volatility_stable = row["atr"] <= ATR_EXPANSION_MAX * row["atr_median50"]
    volume_not_spiking = row["volume"] <= VOLUME_SPIKE_MAX * row["volume_avg20"]
    return range_bound and volatility_stable and volume_not_spiking


def breakout_confirmed(row) -> bool:
    """VWAP+Breakout entry (vwap.py): a genuine breakout ABOVE the VWAP band
    (VWAP as "fair value"), confirmed by a fresh N-day high (real price-
    structure breakout, not just a VWAP-band crossing) and volume proving
    institutions are active. No ADX regime gate — breakouts are supposed to
    happen when something is trending harder than usual, unlike reversion."""
    broke_vwap_band = row["close"] > row["vwap_upper"]
    broke_prior_high = row["close"] > row["prior_high"]
    volume_confirms = row["volume"] >= VOLUME_CONFIRM_MIN * row["volume_avg20"]
    return broke_vwap_band and broke_prior_high and volume_confirms


def reversal_fade_confirmed(row) -> bool:
    """Reversal-fade entry (reversal.py): a genuine gap DOWN (real price
    dislocation, not noise) that shows exhaustion — either a volume spike
    (capitulation selling) or the close recovering into the upper 40% of
    the day's range (buyers stepped back in). Same OR logic as the original
    reversal.py's validated capitulation type, now gated on an actual gap
    instead of a 5-day monotonic decline."""
    gapped_down = row["gap_pct"] <= -GAP_THRESHOLD
    exhaustion_signal = (
        row["volume"] >= REVERSAL_FADE_VOLUME_MIN * row["volume_avg20"]
        or row["close_position_in_range"] >= REVERSAL_FADE_CLOSE_POS_MIN
    )
    return gapped_down and exhaustion_signal


def gap_fade_stop_and_target(row, entry_price: float) -> tuple:
    """Reversal-fade (reversal.py) stop/target: ATR-based stop distance
    (1.5x ATR below entry), fixed 2R target — validated in experimental_bb_
    momentum_hedge.py's reversal_fade mode (76.9% win rate on large-caps).
    Same formula the sandbox's gap_and_go_v2 mode also tested, but only
    reversal.py's entry logic was actually adopted live."""
    stop_distance = GAP_ATR_STOP_MULT * float(row["atr"])
    stop_price = entry_price - stop_distance
    target_price = entry_price + GAP_REWARD_R * stop_distance
    return stop_price, target_price


def breakout_stop_and_target(row, entry_price: float) -> tuple:
    """Stop distance capped at min(distance back to the VWAP band, 0.5x ATR),
    floored at 0.1x ATR so a razor-thin band breakout doesn't oversize the
    position. Target is a fixed 2R (2x the stop distance) — the sandbox
    tested this as a direct read of the strategy's claimed "2R expectancy",
    since our system doesn't support partial scale-outs/trailing stops."""
    vwap_stop_distance = entry_price - float(row["vwap_upper"])
    atr_cap_distance = VWAP_BREAKOUT_ATR_CAP * float(row["atr"])
    stop_distance = max(
        min(vwap_stop_distance, atr_cap_distance),
        VWAP_BREAKOUT_MIN_STOP_ATR * float(row["atr"]),
    )
    stop_price = entry_price - stop_distance
    target_price = entry_price + 2 * stop_distance
    return stop_price, target_price
