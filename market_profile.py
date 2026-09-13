"""Market Profile / Auction Market Theory day-trading strategy (2026-09-02,
user request: "Patrick Nill strategy... implemented in day trading" — 2x
World Trading Championship trader whose publicly-described approach is
built on Auction Market Theory: price behavior around a session's "fair
value," using volume/order-flow tools.

SCOPE NOTE, read this first: this implements the well-established, publicly
documented Auction Market Theory / Market Profile framework (Point of
Control, Value Area, value-area breakout/rejection — originated by J. Peter
Steidlmayer at the CBOT in the 1980s, the standard foundation this style of
trading cites) — NOT Nill's literal proprietary rule set, which lives behind
paywalled mentorship content this project has no access to. His own
material describes a "multi-layer confluence filter" only at a marketing
level, with no reproducible specifics available. Treat this as "AMT-based
day trading, in the spirit of that approach," not a copy of his system.

WHY MINUTE BARS, NOT DAILY (unlike breakout/mean_reversion/vwap/gap_and_go/
reversal): Market Profile is fundamentally an INTRADAY concept — Point of
Control and Value Area are the volume-at-price distribution of actual
trading activity, not a multi-day close/high/low series. Same reasoning as
scalping.py's data choice.

WHY THIS SKIPS BACKTESTING (same guard as scalping.py): Lumibot's
YahooDataBacktesting source can't provide correct minute data — its
_data_store caches a fetched dataframe by asset only, not (asset,
timestep), so once another strategy fetches a symbol's DAILY bars in the
same backtest, a later "minute" request silently returns the cached DAILY
data instead. This would build a volume profile off daily bars mislabeled
as minute bars — fabricated, not just wrong. Never remove this guard; only
live minute data (via Alpaca/IB, bypassing Lumibot's backtest source
entirely) can validate this strategy, same constraint as scalping.
"""
import logging
import numpy as np
import regime_router

logger = logging.getLogger(__name__)

# How much recent minute-bar history builds the volume profile. Roughly two
# US trading sessions (390 min/day) so there's a meaningful profile even
# early in a fresh session, not just today-so-far. Market Profile is
# normally read per-session, not as a single months-long histogram — this
# rolling 2-session window is a practical middle ground for a bot that
# re-evaluates every cycle rather than freezing one profile per day.
PROFILE_LOOKBACK_MINUTES = 780
MIN_BARS_FOR_PROFILE = 120  # need a real sample before trusting POC/VAH/VAL
NUM_PRICE_BINS = 30
VALUE_AREA_PCT = 0.70  # classic Market Profile convention (~1 std dev)

# A breakout above VAH only counts as "acceptance" (not just a probe) with
# volume confirmation — a classic AMT distinction between a genuine auction
# moving into new territory vs. a low-volume excursion that's likely to
# fail back into value.
VOLUME_CONFIRM_MULT = 1.2
VOLUME_LOOKBACK_BARS = 20

# Stop just inside the old value area (below the breakout level, not at
# VAH itself, to avoid getting stopped out by the first small pullback);
# target is a classic AMT "measured move" — the value area's own width
# projected beyond VAH, the standard textbook target for this setup.
STOP_INSIDE_VALUE_AREA_PCT = 0.25  # fraction of value-area width back from VAH

# Risk-normalized position sizing (2026-09-02, validated in
# experimental_market_profile_filters.py against 90 days of real minute
# data): a flat percent-of-portfolio notional meant a volatile day's wider
# value area produced both a proportionally bigger win AND a
# proportionally bigger loss in dollar terms — the top 10 of 475 trades
# (2.1%) carried 92.0% of total profit, a fragile result riding on a
# handful of high-volatility days. Sizing so every trade risks roughly the
# same dollar amount instead dropped that concentration to 47.4% and
# improved total P&L ~14%. RISK_PER_TRADE_PCT_OF_ALLOCATION=0.5% carries
# over the sandbox's own validated ratio (targeted $5 risk against a
# fixed $1000 test notional there) onto whatever capital strategy_manager
# actually allocates live. Deliberately never sizes ABOVE the normal
# allocation (only at or below it) — the sandbox tested uncapped upward
# sizing (up to 5x) for tight-stop days, but that's a bigger live-capital-
# risk tradeoff than this first deployment should make without a
# separate, explicit decision.
RISK_PER_TRADE_PCT_OF_ALLOCATION = 0.005


class MarketProfileStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # Liquid, high-volume names — Market Profile needs real trading
        # volume to be statistically meaningful; thin symbols would produce
        # a noisy, unreliable profile. Same reasoning scalping.py's
        # universe already follows. SPY excluded 2026-09-02 — confirmed a
        # real loser (-$85.92/90d) under risk-normalized sizing, not just
        # noise (see experimental_market_profile_filters.py): SPY's
        # typical price moves are tighter than the other names, so honest
        # risk-based sizing boosts its notional more than its actual edge
        # justifies.
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe). self.symbols is now this cycle's REGIME-ROUTED
        # candidates (see regime_router.py): the shared low-cost-screened
        # pool, filtered to symbols currently in a "bullish" regime
        # (STRATEGY_REGIMES["market_profile"]) — recomputed fresh in
        # analyze() every cycle. Routing uses daily bars for the regime/
        # affordability check even though this strategy's own entries are
        # minute-bar -- neither changes minute to minute, no reason to
        # re-derive from intraday data.
        self.symbols = []
        # symbol -> (stop_price, target_price) for this cycle's BUY signal
        # only — consumed by strategy_manager._run_strategy via
        # get_exit_levels(), same pattern as mean_reversion/vwap/reversal.
        self.exit_levels = {}

    def _compute_value_area(self, df):
        """(poc_price, vah_price, val_price) from a volume-at-price
        histogram — the actual Market Profile calculation: bucket the
        session's traded volume into NUM_PRICE_BINS price bins spanning
        [low, high], find the Point of Control (max-volume bin), then
        expand outward from it — alternately adding whichever adjacent
        bin (above or below) has more volume — until VALUE_AREA_PCT of
        total volume is captured. That's the standard textbook algorithm
        real Market Profile software uses, not an approximation.

        Since only OHLCV bars are available (no actual tick data), each
        bar's volume is distributed across every bin its [low, high]
        range overlaps, weighted by the overlap fraction — a reasonable
        proxy for true volume-at-price, better than crediting a bar's
        whole volume to its close price alone (which would ignore the
        rest of its range entirely).

        Returns (None, None, None) if the window has no real range.
        """
        low, high = float(df["low"].min()), float(df["high"].max())
        if high <= low:
            return None, None, None

        # Fully vectorized (2026-09-02, fixed after a live stall — the
        # original per-bar Python for-loop, called for every symbol every
        # cycle, was a CPU-bound pure-Python loop holding the GIL long
        # enough to make Flask's API thread intermittently unresponsive
        # for several seconds, tripping watchdog's "API unreachable"
        # check and causing real restarts during live trading. Same
        # exact math, just done as one batch of numpy array operations
        # (bars x bins) instead of NUM_PRICE_BINS-sized numpy work
        # repeated once per bar in a Python loop — numpy's C-level
        # broadcasting replaces the per-row Python overhead entirely.
        bin_edges = np.linspace(low, high, NUM_PRICE_BINS + 1)
        bin_lo = bin_edges[:-1]
        bin_hi = bin_edges[1:]

        bar_low = df["low"].to_numpy()
        bar_high = df["high"].to_numpy()
        bar_close = df["close"].to_numpy()
        bar_volume = df["volume"].to_numpy()

        # overlap[i, j] = how much of bar i's [low, high] range falls in bin j
        overlap_start = np.maximum(bar_low[:, None], bin_lo[None, :])
        overlap_end = np.minimum(bar_high[:, None], bin_hi[None, :])
        overlap = np.clip(overlap_end - overlap_start, 0, None)
        total_overlap = overlap.sum(axis=1)

        has_range = total_overlap > 0
        weights = np.divide(overlap, total_overlap[:, None], out=np.zeros_like(overlap), where=has_range[:, None])
        distributed = weights * bar_volume[:, None]
        distributed[bar_volume <= 0] = 0
        bin_volumes = distributed.sum(axis=0)

        # Degenerate bars (high<=low, e.g. a bar with zero range) get their
        # whole volume credited to the bin their close price falls in —
        # same fallback the original loop used.
        degenerate = (~has_range) & (bar_volume > 0)
        if degenerate.any():
            bin_idx = np.clip(
                ((bar_close[degenerate] - low) / (high - low) * NUM_PRICE_BINS).astype(int),
                0, NUM_PRICE_BINS - 1,
            )
            np.add.at(bin_volumes, bin_idx, bar_volume[degenerate])

        if bin_volumes.sum() <= 0:
            return None, None, None

        poc_idx = int(np.argmax(bin_volumes))
        poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

        total_volume = float(bin_volumes.sum())
        target_volume = total_volume * VALUE_AREA_PCT
        cum_volume = bin_volumes[poc_idx]
        lo_idx, hi_idx = poc_idx, poc_idx
        while cum_volume < target_volume and (lo_idx > 0 or hi_idx < NUM_PRICE_BINS - 1):
            vol_below = bin_volumes[lo_idx - 1] if lo_idx > 0 else -1
            vol_above = bin_volumes[hi_idx + 1] if hi_idx < NUM_PRICE_BINS - 1 else -1
            if vol_above >= vol_below:
                hi_idx += 1
                cum_volume += bin_volumes[hi_idx]
            else:
                lo_idx -= 1
                cum_volume += bin_volumes[lo_idx]

        val_price = float(bin_edges[lo_idx])
        vah_price = float(bin_edges[hi_idx + 1])
        return poc_price, vah_price, val_price

    def analyze(self):
        """Two classic Auction Market Theory day-trading patterns:

        BUY — Value Area Breakout: price closes above the Value Area High
        with volume confirmation (VOLUME_CONFIRM_MULT), i.e. the auction
        is being genuinely ACCEPTED in new territory, not just probing it.
        Stop set just inside the old value area; target is the value
        area's own width projected beyond VAH (the standard AMT measured-
        move target for this setup).

        SELL — Rejection back into value: an existing position exits once
        price falls back to/through the Point of Control — the breakout
        has failed and the auction has rotated back toward fair value.
        """
        decisions = {}
        self.exit_levels = {}
        if getattr(self.strategy, "is_backtesting", False):
            return decisions

        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "market_profile")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        for symbol in self.symbols:
            bars = self.strategy.get_historical_prices(symbol, PROFILE_LOOKBACK_MINUTES, "minute")
            if not bars or len(bars.df) < MIN_BARS_FOR_PROFILE:
                continue
            df = bars.df

            poc, vah, val = self._compute_value_area(df)
            if poc is None:
                continue

            current_price = float(df["close"].iloc[-1])
            current_volume = float(df["volume"].iloc[-1])
            avg_volume = float(df["volume"].tail(VOLUME_LOOKBACK_BARS).mean())

            if current_price > vah and avg_volume > 0 and current_volume > avg_volume * VOLUME_CONFIRM_MULT:
                value_area_width = vah - val
                # Anchored to the ACTUAL entry price, not VAH — entry only
                # fires after volume confirmation, so price has usually
                # already drifted above VAH by entry time. Anchoring to
                # VAH silently ate into the target's room while stretching
                # the stop's effective distance from entry, inverting the
                # intended reward:risk especially on faster-moving symbols
                # (confirmed live in sandbox testing 2026-09-02: this
                # single change flipped a flat/breakeven 30-day, 188-trade
                # backtest — $0.00/trade avg — to +$0.27/trade avg, and
                # flipped TSLA specifically from the worst performer
                # (-$49.48) to one of the best (+$27.30). See
                # experimental_market_profile_filters.py / project memory).
                stop_price = current_price - STOP_INSIDE_VALUE_AREA_PCT * value_area_width
                target_price = current_price + value_area_width
                if target_price > current_price > stop_price:
                    decisions[symbol] = "BUY"
                    self.exit_levels[symbol] = (stop_price, target_price)
            elif current_price <= poc:
                decisions[symbol] = "SELL"

        return decisions

    def get_exit_levels(self, symbol):
        """(stop_price, target_price) for symbol's most recent BUY signal,
        or None if it didn't fire one this cycle."""
        return self.exit_levels.get(symbol)

    def get_position_notional(self, symbol, available_capital, current_price, stop_price):
        """Risk-normalized notional for this trade — see
        RISK_PER_TRADE_PCT_OF_ALLOCATION's comment for the validation
        behind this. Sizes so (current_price - stop_price) * shares is
        roughly a fixed fraction of the capital that would normally be
        allocated, regardless of how wide today's value area happens to
        be — never returns MORE than available_capital, only less."""
        if not current_price or not stop_price or stop_price >= current_price:
            return available_capital
        stop_distance_pct = (current_price - stop_price) / current_price
        if stop_distance_pct <= 0:
            return available_capital
        target_risk = RISK_PER_TRADE_PCT_OF_ALLOCATION * available_capital
        target_notional = target_risk / stop_distance_pct
        return min(target_notional, available_capital)
