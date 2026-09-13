import logging
from regime_matcher import symbol_regime_matches
import regime_router
logger = logging.getLogger(__name__)

class GapAndGoStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe). self.symbols is now this cycle's REGIME-ROUTED
        # candidates (see regime_router.py) -- recomputed fresh in
        # analyze() every cycle, not a one-time init.
        self.symbols = []

    def analyze(self):
        """Trade stocks that gap at open. Regime-prefiltered 2026-09-02 —
        see regime_matcher.REGIME_PREFILTER_STRATEGIES for why gap_and_go
        specifically (validated: bullish-regime signals meaningfully
        outperformed non-bullish, unlike breakout/market_profile). Only
        gates a symbol with NO existing position — an already-open
        position must always get its SELL condition evaluated regardless
        of regime, or a real exit could get silently blocked."""
        decisions = {}
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "gap_and_go")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            has_position = bool(self.strategy.get_position(symbol))
            if not has_position and not symbol_regime_matches(self.strategy, "gap_and_go", symbol):
                continue
            bars = self.strategy.get_historical_prices(symbol, 2, "day")
            if not bars or len(bars.df) < 2:
                continue
            prev_close = bars.df["close"].iloc[-2]
            today_open = bars.df["open"].iloc[-1]
            gap = (today_open - prev_close) / prev_close
            if gap > 0.03:  # gap up
                decisions[symbol] = "BUY"
            elif gap < -0.03:  # gap down
                decisions[symbol] = "SELL"
        return decisions
