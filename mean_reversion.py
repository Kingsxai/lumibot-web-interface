import logging
from regime_indicators import compute_indicators, reversion_regime_ok, MIN_BARS_REQUIRED, MINIMAL_TAKE_PROFIT_PCT
import regime_router
logger = logging.getLogger(__name__)

# Fixed 2026-09-01 after backtesting found the ORIGINAL formula (stop =
# bb_lower - atr, target = sma20) had unfavorable reward:risk (0.86:1) even
# before considering win rate — no amount of stop-width retuning alone
# fixed it (tested 0.75x-3.0x ATR, expectancy stayed negative throughout).
# The real problem was the ENTRY: touching the lower Bollinger Band while
# ADX is low doesn't reliably distinguish genuine range-bound reversion
# from a slow grind-down ADX hasn't caught up to yet. Investigated what
# actually separates winners from losers (106 historical signals): 5-day
# momentum leading into the signal was the strongest, cleanest, most
# monotonic differentiator — winners drift in gently (-4.6% avg), losers
# arrive via a sharp recent drop (-8.8% avg, a "falling knife" signature).
# A VWAP filter and a stricter ADX threshold were also tested and didn't
# help (VWAP was a no-op — always true for these entries; tighter ADX
# actually worsened reward:risk despite raising win rate). Combining this
# momentum filter with a clean 2x-ATR stop (replacing the old bb_lower-atr
# formula) got expectancy from -3.04%/trade to -0.44%/trade (n=39) — a
# large improvement, though still not confirmed profitable. A historical
# news-sentiment filter was also tested and found REDUNDANT with momentum
# (near-identical expectancy on a much smaller sample when both required)
# — sharp price drops and bad news are largely the same events here, so
# news doesn't add independent entry-filtering value. News is instead used
# as a portfolio-wide emergency stop (see strategy_manager.py's
# _check_news_emergency_stop) rather than a per-strategy entry filter.
MOMENTUM_5D_FLOOR = -0.06  # skip entries preceded by a sharp recent decline
STOP_ATR_MULT = 2.0        # stop = entry_price - STOP_ATR_MULT * atr (was bb_lower - atr)


class MeanReversionStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe -- the old hand-picked, sandbox-validated list is
        # retired as the scan source). self.symbols is now this cycle's
        # REGIME-ROUTED candidates (see regime_router.py): the shared
        # low-cost-screened pool, filtered to symbols currently in a
        # "sideways" regime (STRATEGY_REGIMES["mean_reversion"]) --
        # recomputed fresh in analyze() every cycle, not a one-time init.
        self.symbols = []
        # symbol -> (stop_price, target_price), computed at the same moment
        # as a BUY signal — see STOP_ATR_MULT comment above for why this is
        # a clean ATR multiple off entry_price now, not the original
        # bb_lower-atr/sma20 formula. Consumed by
        # strategy_manager._run_strategy via get_exit_levels() to pass
        # absolute prices into the bracket order instead of a fixed percent.
        self.exit_levels = {}

    def analyze(self):
        """Bollinger Band mean reversion, gated to genuine range-bound
        conditions AND a momentum filter (see MOMENTUM_5D_FLOOR): buys a
        touch of the lower band only when the approach wasn't a sharp
        recent decline, sells on reversion back to/above the middle band
        (20-day SMA)."""
        decisions = {}
        self.exit_levels = {}
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "mean_reversion")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
            if not bars or len(bars.df) < MIN_BARS_REQUIRED:
                continue
            df = compute_indicators(bars.df)
            row = df.iloc[-1]
            if row[["sma20", "bb_lower", "atr", "adx", "atr_median50", "volume_avg20"]].isna().any():
                continue

            close_5d_ago = df.iloc[-6]["close"]
            momentum_5d = (row["close"] - close_5d_ago) / close_5d_ago

            touched_lower_band = row["close"] <= row["bb_lower"]
            if reversion_regime_ok(row) and touched_lower_band and momentum_5d > MOMENTUM_5D_FLOOR:
                entry_price = float(row["close"])
                stop_price = entry_price - STOP_ATR_MULT * float(row["atr"])
                # 2026-09-09: was float(row["sma20"]) (full reversion to
                # the 20-day mean) -- explicit user request to minimize
                # take-profit while leaving the stop untouched, see
                # MINIMAL_TAKE_PROFIT_PCT's own comment in
                # regime_indicators.py for the full reasoning.
                target_price = entry_price * (1 + MINIMAL_TAKE_PROFIT_PCT)
                if target_price > entry_price > stop_price:
                    decisions[symbol] = "BUY"
                    self.exit_levels[symbol] = (stop_price, target_price)
            elif row["close"] >= row["sma20"]:
                decisions[symbol] = "SELL"
        return decisions

    def get_exit_levels(self, symbol):
        """(stop_price, target_price) for symbol's most recent BUY signal,
        or None if it didn't fire one this cycle."""
        return self.exit_levels.get(symbol)
