import logging
from regime_indicators import (
    compute_indicators, reversal_fade_confirmed, gap_fade_stop_and_target,
    MIN_BARS_REQUIRED, MINIMAL_TAKE_PROFIT_PCT,
)
import regime_router
logger = logging.getLogger(__name__)

class ReversalStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # symbol -> (stop_price, target_price): ATR-based dynamic exit
        # (1.5x ATR stop, fixed 2R target) validated alongside this entry
        # in experimental_bb_momentum_hedge.py — not the global fixed-%
        # bracket order default. Same pattern as mean_reversion.py; see
        # strategy_manager._run_strategy's get_exit_levels() hook.
        self.exit_levels = {}
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe -- the old hand-picked, validated list is retired as
        # the scan source). self.symbols is now this cycle's REGIME-
        # ROUTED candidates (see regime_router.py): the shared low-cost-
        # screened pool, filtered to symbols currently in a "bearish"
        # regime (STRATEGY_REGIMES["reversal"]) — recomputed fresh in
        # analyze() every cycle, not a one-time init.
        self.symbols = []

    def analyze(self):
        """Reversal fade: bets a gap DOWN fails and price bounces. Buys on
        a genuine gap down showing exhaustion (volume spike or a strong
        close-in-range recovery). Sells once the gap is fully filled (price
        recovers back to/above the pre-gap close)."""
        decisions = {}
        self.exit_levels = {}
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "reversal")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
            if not bars or len(bars.df) < MIN_BARS_REQUIRED:
                continue
            df = compute_indicators(bars.df)
            row = df.iloc[-1]
            if row[["gap_pct", "close_position_in_range", "volume_avg20", "prev_close", "atr"]].isna().any():
                continue

            if reversal_fade_confirmed(row):
                decisions[symbol] = "BUY"
                entry_price = float(row["close"])
                stop_price, _ = gap_fade_stop_and_target(row, entry_price)
                # 2026-09-09: was the strategy's own validated 2R target
                # (gap_fade_stop_and_target's second return value) --
                # explicit user request to minimize take-profit while
                # leaving the stop untouched, see MINIMAL_TAKE_PROFIT_PCT's
                # own comment in regime_indicators.py for the full
                # reasoning.
                target_price = entry_price * (1 + MINIMAL_TAKE_PROFIT_PCT)
                self.exit_levels[symbol] = (stop_price, target_price)
            elif row["close"] >= row["prev_close"]:
                decisions[symbol] = "SELL"
        return decisions

    def get_exit_levels(self, symbol):
        """(stop_price, target_price) for symbol's most recent BUY signal,
        or None if it didn't fire one this cycle."""
        return self.exit_levels.get(symbol)
