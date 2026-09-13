import logging
import regime_router
logger = logging.getLogger(__name__)

class BreakoutStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe -- capital's too small to justify 8 separate,
        # overlapping lists). self.symbols is now this cycle's REGIME-
        # ROUTED candidates (see regime_router.py): the shared low-cost-
        # screened pool, filtered to symbols whose current regime matches
        # this strategy's own assignment. Recomputed fresh in analyze()
        # every cycle (regime changes), not a one-time init.
        self.symbols = []

    def analyze(self):
        """Detect breakout above resistance or below support."""
        decisions = {}
        # Held positions stay considered regardless of this cycle's
        # regime routing -- an open position's own SELL condition must
        # still get evaluated even if its regime has since drifted away
        # from breakout's own assignment.
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "breakout")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, 20, "day")
            if not bars or len(bars.df) < 20:
                continue
            high = bars.df["high"].max()
            low = bars.df["low"].min()
            current = bars.df["close"].iloc[-1]
            if current > high * 0.99:  # breakout up
                decisions[symbol] = "BUY"
            elif current < low * 1.01:  # breakdown
                decisions[symbol] = "SELL"
        return decisions
