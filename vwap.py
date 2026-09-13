import logging
from regime_indicators import (
    compute_indicators, breakout_confirmed, breakout_stop_and_target,
    MIN_BARS_REQUIRED, MINIMAL_TAKE_PROFIT_PCT,
)
import regime_router
logger = logging.getLogger(__name__)

class VWAPStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # symbol -> (stop_price, target_price): ATR/VWAP-band-derived
        # dynamic exit (breakout_stop_and_target) validated alongside this
        # entry in experimental_bb_momentum_hedge.py — was defined in
        # regime_indicators.py since 2026-08-30 but never actually called;
        # this strategy was silently running on the global fixed-% bracket
        # order default instead. Same fix class as mean_reversion.py's.
        self.exit_levels = {}
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe -- the old hand-picked, validated list is retired as
        # the scan source). self.symbols is now this cycle's REGIME-
        # ROUTED candidates (see regime_router.py): the shared low-cost-
        # screened pool, filtered to symbols currently in a "bullish"
        # regime (STRATEGY_REGIMES["vwap"]) — recomputed fresh in
        # analyze() every cycle, not a one-time init.
        self.symbols = []

    def analyze(self):
        """VWAP + Breakout: buys a genuine breakout above the VWAP band
        (VWAP as institutional "fair value"), confirmed by a fresh N-day
        high and volume proving institutions are active. Sells when price
        falls back inside the band (breakout failed)."""
        decisions = {}
        self.exit_levels = {}
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "vwap")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, MIN_BARS_REQUIRED, "day")
            if not bars or len(bars.df) < MIN_BARS_REQUIRED:
                continue
            df = compute_indicators(bars.df)
            row = df.iloc[-1]
            if row[["vwap_upper", "prior_high", "atr", "volume_avg20"]].isna().any():
                continue

            if breakout_confirmed(row):
                decisions[symbol] = "BUY"
                entry_price = float(row["close"])
                stop_price, _ = breakout_stop_and_target(row, entry_price)
                # 2026-09-09: was the strategy's own validated 2R target
                # (breakout_stop_and_target's second return value) --
                # explicit user request to minimize take-profit while
                # leaving the stop untouched, see MINIMAL_TAKE_PROFIT_PCT's
                # own comment in regime_indicators.py for the full
                # reasoning.
                target_price = entry_price * (1 + MINIMAL_TAKE_PROFIT_PCT)
                self.exit_levels[symbol] = (stop_price, target_price)
            elif row["close"] < row["vwap_upper"]:
                decisions[symbol] = "SELL"
        return decisions

    def get_exit_levels(self, symbol):
        """(stop_price, target_price) for symbol's most recent BUY signal,
        or None if it didn't fire one this cycle."""
        return self.exit_levels.get(symbol)
