"""14 new live strategies (2026-09-11), ported from this session's marathon
backtest adapters (marathon_transcript_batch1-5.py,
sandbox_breakout_chandelier_backtest.py) into live-trading form, following
market_profile.py's established pattern: `__init__(self, strategy)`,
`analyze() -> {symbol: "BUY"}`, optional `get_exit_levels(symbol)` and
`get_position_notional(...)`.

TRANSLATION APPROACH: the backtest adapters scan a full historical
DataFrame with `for i in range(5, n): ...`, tracking multi-bar state
(active_zone, deadline countdowns, etc.) across that whole loop. Live
trading instead recomputes each strategy's own indicators/fractals/zones
FRESH every cycle over a bounded lookback window (same pattern
market_profile.py already uses for its own value-area calc) and asks only
"does the MOST RECENT bar satisfy this strategy's entry condition, given
whatever zone/swing state is currently active?" -- functionally
equivalent to the backtest logic without needing to persist a multi-bar
state machine between cycles. Existing-position/re-entry guarding and
exclusive-ownership locking are handled centrally by strategy_manager.py
(_place_bracket_order / _close_position), same as every other strategy.

SEEDED OFF BY DEFAULT (signal_logger.ALL_STRATEGY_NAMES) -- nothing here
trades until a human enables it per-strategy on the dashboard.

Symbol universe: hourly_universe_screener.get_rotating_universe() (MACD+
RSI+volume bullish, ranked by ATR%), not regime_router -- these are
momentum/volatility strategies, not the ADX-trend-regime style the
original 8 strategies route through.

2026-09-12: a controlled filtered-vs-unfiltered backtest (all 16 currently
live-enabled strategies, real per-strategy sizing, Aug 24-28 week) found
the regime+chaos filter (regime_matcher.symbol_regime_matches +
regime_router._is_chaotic -- the same two checks scalping.py's real
production universe applies) helps only 4 of 16: scalping,
ema_ribbon_smi, volume_divergence_grab, candle_taxonomy. It hurts the
other 12, in two cases turning a profitable strategy into a loser
(supertrend_200ema, volume_absorption). So it's applied here ONLY to the
3 extended strategies it actually helped -- see
`use_regime_chaos_filter` below -- the other 11 keep their original,
unfiltered hourly_universe_screener candidate list.
"""
import logging

import numpy as np
import pandas as pd

import hourly_universe_screener
from regime_matcher import symbol_regime_matches
from regime_router import _is_chaotic

logger = logging.getLogger(__name__)

FRACTAL_PERIOD = 2
ATR_PERIOD = 14
ZONE_MAX_AGE_BARS = 60
LOOKBACK_MINUTE_BARS = 600

# ATR-style risk-normalized sizing (market_profile.py's own pattern,
# reused verbatim) -- each strategy's own RISK_PCT tuned to roughly match
# its winning ATR risk-% level from the marathon backtest (see the Sizing
# Atlas artifact). Never sizes ABOVE the normal allocation, only at/below.
RISK_PCT_DEFAULT = 0.02


def _atr(df, period=ATR_PERIOD):
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _find_fractals(df, period=FRACTAL_PERIOD):
    """(is_low, is_high) boolean Series -- True on the bar that IS the
    fractal extreme, confirmed `period` bars later by the caller (same
    convention as every SMC-family backtest this session)."""
    n = len(df)
    low, high = df["low"].values, df["high"].values
    is_low = np.zeros(n, dtype=bool)
    is_high = np.zeros(n, dtype=bool)
    for i in range(period, n - period):
        wl = low[i - period:i + period + 1]
        wh = high[i - period:i + period + 1]
        if low[i] == wl.min() and np.sum(wl == wl.min()) == 1:
            is_low[i] = True
        if high[i] == wh.max() and np.sum(wh == wh.max()) == 1:
            is_high[i] = True
    return pd.Series(is_low, index=df.index), pd.Series(is_high, index=df.index)


def _find_order_block_zones(df):
    """Last opposite-color candle before a >2xATR impulsive move -- same
    detector shared across order_blocks/supply_demand/discount_zone/
    rigorous_rr/volume_absorption/candle_taxonomy this session. Returns
    [(bar_index, zone_low, zone_high), ...] sorted oldest-first."""
    n = len(df)
    atr = _atr(df)
    zones = []
    for i in range(15, n - 3):
        is_down = df["close"].iloc[i] < df["open"].iloc[i]
        if not is_down:
            continue
        move = df["close"].iloc[i + 3] - df["close"].iloc[i]
        if pd.notna(atr.iloc[i]) and atr.iloc[i] > 0 and move > 2 * atr.iloc[i]:
            zones.append((i, float(df["low"].iloc[i]), float(df["high"].iloc[i])))
    return zones


def _last_swing_low_high(df, is_low, is_high, confirm_offset=FRACTAL_PERIOD):
    """Most recent confirmed swing low/high as of the last bar (mirrors
    the backtests' `if confirm_idx >= 0 and is_low[confirm_idx]: last_low
    = ...` running update, evaluated once at the end instead of per-bar)."""
    n = len(df)
    last_confirm_idx = n - 1 - confirm_offset
    last_low = last_high = None
    for idx in range(last_confirm_idx, -1, -1):
        if last_low is None and is_low.iloc[idx]:
            last_low = float(df["low"].iloc[idx])
        if last_high is None and is_high.iloc[idx]:
            last_high = float(df["high"].iloc[idx])
        if last_low is not None and last_high is not None:
            break
    return last_low, last_high


def _atr_position_notional(available_capital, current_price, stop_price, risk_pct):
    if not current_price or not stop_price or stop_price >= current_price:
        return available_capital
    stop_distance_pct = (current_price - stop_price) / current_price
    if stop_distance_pct <= 0:
        return available_capital
    target_risk = risk_pct * available_capital
    target_notional = target_risk / stop_distance_pct
    return min(target_notional, available_capital)


class _BaseExtendedStrategy:
    """Shared scaffolding: symbol universe, backtest guard, exit-level
    storage -- every subclass implements `_check_entry(df, symbol)` ->
    (stop_price, target_price) | None."""

    name = "base"
    lookback_bars = LOOKBACK_MINUTE_BARS
    timestep = "minute"
    min_bars = 250
    # 2026-09-12: opt-in, per-strategy -- see the module docstring above
    # for why only 3 of the 14 set this True. Held positions are never
    # filtered (same convention scalping.py already uses), only new
    # candidates from hourly_universe_screener.
    use_regime_chaos_filter = False

    def __init__(self, strategy):
        self.strategy = strategy
        self.symbols = []
        self.exit_levels = {}

    def analyze(self):
        decisions = {}
        self.exit_levels = {}
        if getattr(self.strategy, "is_backtesting", False):
            return decisions

        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = hourly_universe_screener.get_rotating_universe(self.strategy)
        if self.use_regime_chaos_filter:
            routed = [
                s for s in routed
                if symbol_regime_matches(self.strategy, self.name, s)
                and not _is_chaotic(self.strategy, s)
            ]
        self.symbols = list(dict.fromkeys(routed + held_symbols))

        for symbol in self.symbols:
            try:
                bars = self.strategy.get_historical_prices(symbol, self.lookback_bars, self.timestep)
                if not bars or len(bars.df) < self.min_bars:
                    continue
                df = bars.df
                result = self._check_entry(df, symbol)
                if result is None:
                    continue
                stop_price, target_price = result
                current_price = float(df["close"].iloc[-1])
                if not (target_price > current_price > stop_price):
                    continue
                decisions[symbol] = "BUY"
                self.exit_levels[symbol] = (stop_price, target_price)
            except Exception as e:
                logger.warning(f"{self.name}: error analyzing {symbol}: {e}")

        return decisions

    def get_exit_levels(self, symbol):
        return self.exit_levels.get(symbol)


# ---------------------------------------------------------------------
# ATR-style sizing (10 of the 14) -- reuse market_profile.py's exact hook
# ---------------------------------------------------------------------
class _AtrSizedStrategy(_BaseExtendedStrategy):
    risk_pct = RISK_PCT_DEFAULT

    def get_position_notional(self, symbol, available_capital, current_price, stop_price):
        return _atr_position_notional(available_capital, current_price, stop_price, self.risk_pct)


# ---------------------------------------------------------------------
# lab_range -- two-sided liquidity sweep defines a range, enter inside it
# ---------------------------------------------------------------------
class LabRangeStrategy(_AtrSizedStrategy):
    name = "lab_range"
    risk_pct = 0.03  # backtest's winning ATR level was 3%

    def _check_entry(self, df, symbol):
        is_low, is_high = _find_fractals(df)
        last_swing_low, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_low is None or last_swing_high is None:
            return None
        n = len(df)
        row = df.iloc[-1]
        # A lower sweep followed (within 20 bars) by an upper sweep defines the range.
        window = df.iloc[max(0, n - 25):]
        lower_sweeps = window[(window["low"] < last_swing_low) & (window["close"] > last_swing_low)]
        if lower_sweeps.empty:
            return None
        sweep_pos = window.index.get_loc(lower_sweeps.index[0])
        upper_window = window.iloc[sweep_pos:sweep_pos + 21]
        upper_sweeps = upper_window[(upper_window["high"] > last_swing_high) & (upper_window["close"] < last_swing_high)]
        if upper_sweeps.empty:
            return None
        range_low = float(lower_sweeps["low"].iloc[0])
        range_high = float(upper_sweeps["high"].iloc[0])
        mid = (range_low + range_high) / 2
        if row["close"] <= mid and row["close"] > range_low and row["close"] > row["open"]:
            return range_low, range_high
        return None


# ---------------------------------------------------------------------
# supply_demand -- order-block zone touch, target = the move's own high
# ---------------------------------------------------------------------
class SupplyDemandStrategy(_AtrSizedStrategy):
    name = "supply_demand"
    risk_pct = 0.01

    def _check_entry(self, df, symbol):
        zones = _find_order_block_zones(df)
        if not zones:
            return None
        n = len(df)
        formed_idx, zlow, zhigh = zones[-1]
        if n - 1 - formed_idx > ZONE_MAX_AGE_BARS:
            return None
        row = df.iloc[-1]
        move_high = float(df["high"].iloc[formed_idx:min(formed_idx + 4, n)].max())
        touched = row["low"] <= zhigh
        if touched and row["close"] > row["open"] and row["close"] >= zlow and move_high > row["close"]:
            return zlow * 0.999, move_high
        return None


# ---------------------------------------------------------------------
# supertrend_200ema -- SuperTrend flips bullish above EMA200
# ---------------------------------------------------------------------
class Supertrend200EmaStrategy(_AtrSizedStrategy):
    name = "supertrend_200ema"
    risk_pct = 0.02
    min_bars = 260

    @staticmethod
    def _supertrend(df, atr_period=10, multiplier=3.0):
        # Vectorized-array rewrite (2026-09-12): the original loop wrote
        # every element via pandas Series.iloc[i] (4 separate Series)
        # inside a plain Python for-loop -- slow and memory-heavy at scale.
        # Same algorithm, numpy arrays only in the hot loop; returned as
        # pd.Series so callers are unaffected. See
        # marathon_transcript_batch2.py's identical fix for the backtest copy.
        atr = _atr(df, atr_period).to_numpy()
        hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        close = df["close"].to_numpy()
        n = len(df)
        final_upper = np.full(n, np.nan)
        final_lower = np.full(n, np.nan)
        supertrend = np.full(n, np.nan)
        trend = np.ones(n, dtype="int64")
        started = False
        for i in range(n):
            if np.isnan(atr[i]):
                continue
            if not started:
                final_upper[i] = basic_upper[i]
                final_lower[i] = basic_lower[i]
                trend[i] = 1
                supertrend[i] = final_lower[i]
                started = True
                continue
            final_upper[i] = basic_upper[i] if (basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
            final_lower[i] = basic_lower[i] if (basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
            prev_st = supertrend[i - 1]
            if prev_st == final_upper[i - 1]:
                if close[i] > final_upper[i]:
                    trend[i] = 1; supertrend[i] = final_lower[i]
                else:
                    trend[i] = -1; supertrend[i] = final_upper[i]
            else:
                if close[i] < final_lower[i]:
                    trend[i] = -1; supertrend[i] = final_upper[i]
                else:
                    trend[i] = 1; supertrend[i] = final_lower[i]
        return pd.Series(trend, index=df.index), pd.Series(supertrend, index=df.index)

    def _check_entry(self, df, symbol):
        ema200 = df["close"].ewm(span=200, adjust=False).mean()
        trend, line = self._supertrend(df)
        flipped_up = trend.iloc[-2] == -1 and trend.iloc[-1] == 1
        row = df.iloc[-1]
        if flipped_up and row["close"] > ema200.iloc[-1]:
            stop = float(line.iloc[-1])
            if stop < row["close"]:
                risk = row["close"] - stop
                return stop, float(row["close"] + 1.5 * risk)
        return None


# ---------------------------------------------------------------------
# fakeout_breakout_fib -- liquidity fakeout + genuine breakout, enter on
# the golden-zone (0.618-0.79) retracement
# ---------------------------------------------------------------------
class FakeoutBreakoutFibStrategy(_AtrSizedStrategy):
    name = "fakeout_breakout_fib"
    risk_pct = 0.02

    def _check_entry(self, df, symbol):
        is_low, is_high = _find_fractals(df)
        n = len(df)
        window = df.iloc[max(0, n - 100):]
        wlow, whigh = is_low.iloc[max(0, n - 100):], is_high.iloc[max(0, n - 100):]
        last_low = last_high = None
        used_low_val = None
        fakeout_low = None
        for pos in range(len(window)):
            row = window.iloc[pos]
            if wlow.iloc[pos]:
                last_low = float(row["low"])
            if whigh.iloc[pos]:
                last_high = float(row["high"])
            if last_low is not None and row["low"] < last_low and row["close"] > last_low and used_low_val != last_low:
                used_low_val = last_low
                fakeout_low = float(row["low"])
        if fakeout_low is None or last_high is None:
            return None
        breakout_rows = window[window["close"] > last_high]
        if breakout_rows.empty:
            return None
        breakout_high = float(breakout_rows["high"].iloc[-1])
        rng = breakout_high - fakeout_low
        if rng <= 0:
            return None
        zone_low = breakout_high - 0.79 * rng
        zone_high = breakout_high - 0.618 * rng
        row = df.iloc[-1]
        if zone_low <= row["low"] <= zone_high:
            stop = fakeout_low * 0.999
            if stop < row["close"] < breakout_high:
                return stop, breakout_high
        return None


# ---------------------------------------------------------------------
# macd_200ema -- zero-line MACD cross below EMA200-confirmed uptrend
# ---------------------------------------------------------------------
class Macd200EmaStrategy(_AtrSizedStrategy):
    name = "macd_200ema"
    risk_pct = 0.02
    min_bars = 220

    @staticmethod
    def _adx(df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        atr = _atr(df, period)
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1 / period, adjust=False).mean()

    def _check_entry(self, df, symbol):
        close = df["close"]
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        adx = self._adx(df)
        row = df.iloc[-1]
        crossed_up = macd_line.iloc[-2] <= signal_line.iloc[-2] and macd_line.iloc[-1] > signal_line.iloc[-1]
        right_side = macd_line.iloc[-1] < 0
        above_ema = row["close"] > ema200.iloc[-1]
        trending = adx.iloc[-1] > 20 if pd.notna(adx.iloc[-1]) else False
        if crossed_up and right_side and above_ema and trending:
            stop = float(ema200.iloc[-1])
            if stop < row["close"]:
                risk = row["close"] - stop
                return stop, float(row["close"] + 1.5 * risk)
        return None


# ---------------------------------------------------------------------
# ema_ribbon_smi -- 13/21/34/55 EMA ribbon bullish + SMI cross from oversold
# ---------------------------------------------------------------------
class EmaRibbonSmiStrategy(_AtrSizedStrategy):
    name = "ema_ribbon_smi"
    risk_pct = 0.01
    min_bars = 80
    use_regime_chaos_filter = True  # 2026-09-12: one of the 4 the filter helped

    @staticmethod
    def _smi(df, k_period=10, d_period=3, ema_period=3):
        hh = df["high"].rolling(k_period).max()
        ll = df["low"].rolling(k_period).min()
        mid = (hh + ll) / 2
        diff = df["close"] - mid
        rng = (hh - ll)
        avg_diff = diff.ewm(span=d_period, adjust=False).mean().ewm(span=d_period, adjust=False).mean()
        avg_rng = rng.ewm(span=d_period, adjust=False).mean().ewm(span=d_period, adjust=False).mean()
        smi = 100 * (avg_diff / (avg_rng / 2).replace(0, np.nan))
        smi_signal = smi.ewm(span=ema_period, adjust=False).mean()
        return smi, smi_signal

    def _check_entry(self, df, symbol):
        close = df["close"]
        e13, e21, e34, e55 = (close.ewm(span=s, adjust=False).mean() for s in (13, 21, 34, 55))
        smi, smi_signal = self._smi(df)
        is_low, _ = _find_fractals(df)
        last_swing_low, _ = _last_swing_low_high(df, is_low, is_low)
        if last_swing_low is None:
            return None
        row = df.iloc[-1]
        ribbon_bullish = e13.iloc[-1] > e21.iloc[-1] > e34.iloc[-1] > e55.iloc[-1]
        smi_cross_up = smi.iloc[-2] <= smi_signal.iloc[-2] and smi.iloc[-1] > smi_signal.iloc[-1] and smi.iloc[-2] < -40
        if ribbon_bullish and smi_cross_up and last_swing_low < row["close"]:
            risk = row["close"] - last_swing_low
            return last_swing_low, float(row["close"] + 2.0 * risk)
        return None


# ---------------------------------------------------------------------
# discount_zone -- order-block zone below the 50% retracement of the
# most recent swing move
# ---------------------------------------------------------------------
class DiscountZoneStrategy(_AtrSizedStrategy):
    name = "discount_zone"
    risk_pct = 0.03

    def _check_entry(self, df, symbol):
        zones = _find_order_block_zones(df)
        if not zones:
            return None
        is_low, is_high = _find_fractals(df)
        last_swing_low, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_low is None or last_swing_high is None:
            return None
        mid = (last_swing_high + last_swing_low) / 2
        n = len(df)
        formed_idx, zlow, zhigh = zones[-1]
        if n - 1 - formed_idx > ZONE_MAX_AGE_BARS or zhigh >= mid:
            return None
        row = df.iloc[-1]
        touched = row["low"] <= zhigh
        if touched and row["close"] > row["open"] and row["close"] >= zlow:
            stop = zlow * 0.999
            if stop < row["close"] < last_swing_high:
                return stop, last_swing_high
        return None


# ---------------------------------------------------------------------
# breakout_chandelier -- momentum-candle break of rolling resistance.
# NOTE: the live bracket-order system only supports a fixed stop/target
# set AT ENTRY (strategy_manager._check_bracket_orders checks fixed
# absolute prices, no per-cycle trailing-stop callback into the
# strategy) -- true Chandelier trailing isn't supported without a much
# larger change to that shared exit-checking machinery, out of scope
# here. Simplified to a fixed bracket: stop = resistance, target = the
# backtest's own 1.5R partial-take level used as the full target instead.
# ---------------------------------------------------------------------
class BreakoutChandelierStrategy(_BaseExtendedStrategy):
    name = "breakout_chandelier"
    min_bars = 80
    RESISTANCE_LOOKBACK = 60
    BIG_BODY_RATIO = 0.70
    TP_R_MULT = 1.5

    def _check_entry(self, df, symbol):
        resistance = df["high"].rolling(self.RESISTANCE_LOOKBACK).max().shift(1)
        body_ratio = (df["close"] - df["open"]).abs() / (df["high"] - df["low"]).clip(lower=1e-9)
        is_up = df["close"] > df["open"]
        row = df.iloc[-1]
        res = resistance.iloc[-1]
        if pd.isna(res):
            return None
        momentum_single = body_ratio.iloc[-1] >= self.BIG_BODY_RATIO and row["close"] > res and is_up.iloc[-1]
        momentum_three = is_up.iloc[-3] and is_up.iloc[-2] and is_up.iloc[-1] and row["close"] > res
        if momentum_single or momentum_three:
            stop = res * 0.999
            if stop < row["close"]:
                risk = row["close"] - stop
                return stop, float(row["close"] + self.TP_R_MULT * risk)
        return None
    # Ride-and-skim sizing: no get_position_notional override -- flat
    # available_capital, relying on the live bot's existing hard $250
    # ceiling + per-cycle reservation to produce the "many smaller
    # concurrent positions" effect this method is built around.


# ---------------------------------------------------------------------
# volume_divergence_grab -- liquidity grab confirmed by below-average
# volume on the sweep candle (ablation on liquidity_grab)
# ---------------------------------------------------------------------
class VolumeDivergenceGrabStrategy(_AtrSizedStrategy):
    name = "volume_divergence_grab"
    risk_pct = 0.02
    use_regime_chaos_filter = True  # 2026-09-12: one of the 4 the filter helped

    def _check_entry(self, df, symbol):
        is_low, is_high = _find_fractals(df)
        last_swing_low, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_low is None or last_swing_high is None:
            return None
        vol_avg = df["volume"].rolling(20).mean()
        row = df.iloc[-1]
        volume_mult = 0.8
        grabbed = row["low"] < last_swing_low and row["close"] > last_swing_low
        low_volume = pd.notna(vol_avg.iloc[-1]) and row["volume"] < vol_avg.iloc[-1] * volume_mult
        if grabbed and low_volume and last_swing_high > row["close"]:
            stop = float(row["low"]) * 0.999
            return stop, last_swing_high
        return None


# ---------------------------------------------------------------------
# asymmetric_dual -- a weak (wick-only) poke above a swing high shortly
# before a strong (body-close) liquidity grab below a swing low
# ---------------------------------------------------------------------
class AsymmetricDualStrategy(_AtrSizedStrategy):
    name = "asymmetric_dual"
    risk_pct = 0.01

    def _check_entry(self, df, symbol):
        is_low, is_high = _find_fractals(df)
        n = len(df)
        window = df.iloc[max(0, n - 100):]
        wlow, whigh = is_low.iloc[max(0, n - 100):], is_high.iloc[max(0, n - 100):]
        last_swing_low = last_swing_high = None
        weak_poke = False
        for pos in range(len(window)):
            row = window.iloc[pos]
            if wlow.iloc[pos]:
                last_swing_low = float(row["low"])
            if whigh.iloc[pos]:
                last_swing_high = float(row["high"])
            if last_swing_high is not None and row["high"] > last_swing_high and row["close"] < last_swing_high:
                weak_poke = True
        if not weak_poke or last_swing_low is None or last_swing_high is None:
            return None
        row = df.iloc[-1]
        grabbed = row["low"] < last_swing_low and row["close"] > last_swing_low
        if grabbed:
            stop = float(row["low"]) * 0.9995
            target = last_swing_high if last_swing_high > row["close"] else float(row["close"]) * 1.02
            if stop < row["close"] < target:
                return stop, target
        return None


# ---------------------------------------------------------------------
# rigorous_rr -- order-block entry gated by a hard risk:reward > 2.5 filter
# ---------------------------------------------------------------------
class RigorousRrStrategy(_AtrSizedStrategy):
    name = "rigorous_rr"
    risk_pct = 0.01

    def _check_entry(self, df, symbol):
        zones = _find_order_block_zones(df)
        if not zones:
            return None
        is_low, is_high = _find_fractals(df)
        _, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_high is None:
            return None
        n = len(df)
        formed_idx, zlow, zhigh = zones[-1]
        if n - 1 - formed_idx > ZONE_MAX_AGE_BARS:
            return None
        row = df.iloc[-1]
        touched = row["low"] <= zhigh
        if touched and row["close"] > row["open"] and row["close"] >= zlow:
            stop = zlow * 0.999
            if stop < row["close"] < last_swing_high:
                rr = (last_swing_high - row["close"]) / (row["close"] - stop)
                if rr > 2.5:
                    return stop, last_swing_high
        return None


# ---------------------------------------------------------------------
# volume_absorption -- order-block zone touch confirmed by above-average
# volume on the return candle
# ---------------------------------------------------------------------
class VolumeAbsorptionStrategy(_AtrSizedStrategy):
    name = "volume_absorption"
    risk_pct = 0.03

    def _check_entry(self, df, symbol):
        zones = _find_order_block_zones(df)
        if not zones:
            return None
        is_low, is_high = _find_fractals(df)
        _, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_high is None:
            return None
        vol_avg = df["volume"].rolling(20).mean()
        n = len(df)
        formed_idx, zlow, zhigh = zones[-1]
        if n - 1 - formed_idx > ZONE_MAX_AGE_BARS:
            return None
        row = df.iloc[-1]
        touched = row["low"] <= zhigh
        volume_confirmed = pd.notna(vol_avg.iloc[-1]) and row["volume"] > vol_avg.iloc[-1] * 1.3
        if touched and row["close"] > row["open"] and row["close"] >= zlow and volume_confirmed:
            stop = zlow * 0.999
            if stop < row["close"] < last_swing_high:
                return stop, last_swing_high
        return None


# ---------------------------------------------------------------------
# candle_taxonomy -- order-block zone touch confirmed by a "control-
# shift"/"strength" candle following an "indecision" candle
# ---------------------------------------------------------------------
class CandleTaxonomyStrategy(_BaseExtendedStrategy):
    name = "candle_taxonomy"
    KELLY_FRACTION = 0.22  # this strategy's own backtest Kelly fraction, sanity-capped
    use_regime_chaos_filter = True  # 2026-09-12: one of the 4 the filter helped

    def _check_entry(self, df, symbol):
        zones = _find_order_block_zones(df)
        if not zones:
            return None
        is_low, is_high = _find_fractals(df)
        _, last_swing_high = _last_swing_low_high(df, is_low, is_high)
        if last_swing_high is None:
            return None
        body = (df["close"] - df["open"]).abs()
        rng = (df["high"] - df["low"]).clip(lower=1e-9)
        body_ratio = body / rng
        upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
        lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
        max_wick_ratio = pd.concat([upper_wick, lower_wick], axis=1).max(axis=1) / rng
        is_indecision = body_ratio <= 0.30
        is_confirm = ((max_wick_ratio >= 0.60) | (body_ratio >= 0.70)) & (df["close"] > df["open"])
        n = len(df)
        formed_idx, zlow, zhigh = zones[-1]
        if n - 1 - formed_idx > ZONE_MAX_AGE_BARS:
            return None
        row = df.iloc[-1]
        touched = row["low"] <= zhigh
        if touched and is_confirm.iloc[-1] and is_indecision.iloc[-2] and row["close"] >= zlow:
            stop = zlow * 0.999
            if stop < row["close"] < last_swing_high:
                return stop, last_swing_high
        return None

    def get_position_notional(self, symbol, available_capital, current_price, stop_price):
        # Kelly-style: flat fraction of the pool, capped at available_capital.
        return min(self.KELLY_FRACTION * available_capital, available_capital)


# ---------------------------------------------------------------------
# volume_profile -- mean reversion off a rolling volume-at-price profile:
# price dips below the Value Area Low, then reclaims it
# ---------------------------------------------------------------------
class VolumeProfileStrategy(_BaseExtendedStrategy):
    name = "volume_profile"
    min_bars = 250
    NUM_PRICE_BINS = 30
    VALUE_AREA_PCT = 0.70

    def __init__(self, strategy):
        super().__init__(strategy)
        self._was_below_val = {}

    @staticmethod
    def _compute_value_area(df, num_bins):
        low, high = float(df["low"].min()), float(df["high"].max())
        if high <= low:
            return None, None, None
        bin_edges = np.linspace(low, high, num_bins + 1)
        bin_lo, bin_hi = bin_edges[:-1], bin_edges[1:]
        bar_low, bar_high = df["low"].to_numpy(), df["high"].to_numpy()
        bar_volume = df["volume"].to_numpy()
        overlap_start = np.maximum(bar_low[:, None], bin_lo[None, :])
        overlap_end = np.minimum(bar_high[:, None], bin_hi[None, :])
        overlap = np.clip(overlap_end - overlap_start, 0, None)
        total_overlap = overlap.sum(axis=1)
        has_range = total_overlap > 0
        weights = np.divide(overlap, total_overlap[:, None], out=np.zeros_like(overlap), where=has_range[:, None])
        bin_volumes = (weights * bar_volume[:, None]).sum(axis=0)
        if bin_volumes.sum() <= 0:
            return None, None, None
        poc_idx = int(np.argmax(bin_volumes))
        target_volume = bin_volumes.sum() * 0.70
        cum_volume = bin_volumes[poc_idx]
        lo_idx = hi_idx = poc_idx
        while cum_volume < target_volume and (lo_idx > 0 or hi_idx < num_bins - 1):
            vol_below = bin_volumes[lo_idx - 1] if lo_idx > 0 else -1
            vol_above = bin_volumes[hi_idx + 1] if hi_idx < num_bins - 1 else -1
            if vol_above >= vol_below:
                hi_idx += 1; cum_volume += bin_volumes[hi_idx]
            else:
                lo_idx -= 1; cum_volume += bin_volumes[lo_idx]
        return float(bin_edges[poc_idx]), float(bin_edges[hi_idx + 1]), float(bin_edges[lo_idx])

    def _check_entry(self, df, symbol):
        poc, vah, val = self._compute_value_area(df, self.NUM_PRICE_BINS)
        if poc is None:
            return None
        row = df.iloc[-1]
        close_now = float(row["close"])
        was_below = self._was_below_val.get(symbol, False)
        if close_now < val:
            self._was_below_val[symbol] = True
            return None
        if was_below and close_now >= val:
            self._was_below_val[symbol] = False
            stop = float(df["low"].tail(5).min()) * 0.999
            if stop < close_now < vah:
                return stop, vah
        self._was_below_val[symbol] = False
        return None
    # Ride-and-skim sizing: no get_position_notional override, same
    # reasoning as breakout_chandelier above.
