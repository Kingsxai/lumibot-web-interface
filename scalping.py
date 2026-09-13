import logging
import config
from regime_matcher import symbol_regime_matches
import regime_router
logger = logging.getLogger(__name__)

# Tighter exit for sentiment-rider-sourced entries only (2026-09-01, user
# call) — this account's Alpaca news tier is assumed DELAYED, not real-time
# (see news_sentiment.py's module docstring), so by the time a symbol clears
# the relevance bar, the price move the news caused may already be partly or
# fully played out. That's a materially different, staler entry than a
# base-universe entry reacting to a real-time price tick, so it gets its own
# (smaller, same ~1.67:1 reward:risk shape as the base 0.3%/0.5%) exit
# instead of sharing scalping's uniform per-strategy risk% — less room
# presumed left to run in either direction, so cut both faster. Starting
# estimate, not yet backtest-validated the way the base risk numbers were;
# revisit with backtest_scalping_sentiment_historical.py if this matters.
RIDER_STOP_LOSS_PERCENT = 0.0015
RIDER_TAKE_PROFIT_PERCENT = 0.0025


class ScalpingStrategy:
    def __init__(self, strategy):
        self.strategy = strategy
        # 2026-09-09 (explicit user request: no more per-strategy fixed
        # universe -- the old separate "fast lane" concept is retired
        # too, since it did the exact same low-cost screening
        # regime_router.py now does for every strategy). self.symbols is
        # this cycle's REGIME-ROUTED candidates: the shared low-cost-
        # screened pool, filtered to symbols currently in a "bullish"
        # regime (STRATEGY_REGIMES["scalping"]) — recomputed fresh in
        # analyze() every cycle, not a one-time init.
        self.symbols = []
        # symbol -> (stop_price, target_price) for this cycle's rider-
        # sourced BUY signals only — base-universe BUYs aren't in here, so
        # get_exit_levels() falls back to the uniform per-strategy risk%
        # for those, same as before this existed.
        self.exit_levels = {}
        # symbol -> sentiment score for this cycle's rider-sourced BUY
        # signals only, so strategy_manager can log a human-readable reason
        # ("News sentiment spike, score 0.42") alongside the signal instead
        # of just "scalping BUY" with no indication the news rider fired it.
        self.rider_reasons = {}

    def _sentiment_candidates(self) -> list:
        """Symbols from config.NEWS_SENTIMENT_UNIVERSE with a current
        positive sentiment reading, as extra scan candidates for this cycle
        only (not merged into self.symbols — this is about catching TODAY's
        news-driven mover, not a permanent universe change).

        Design decision (2026-09-01, user call): sentiment is NOT its own
        trading strategy anymore — news_sentiment.py's old standalone
        BUY/SELL logic had no price-action confirmation and no exit
        strategy of its own (unlike mean_reversion/vwap/reversal's
        get_exit_levels()). A news-driven sentiment spike IS exactly the
        kind of short intraday burst scalping already looks for, so
        sentiment is used here purely as a symbol-selection input —
        scalping's own existing 0.2% entry trigger and existing exit
        (global/per-strategy stop-loss/take-profit, unchanged) still fully
        govern whatever happens to these symbols. Reuses
        NewsSentimentAnalyzer's own 1-hour per-symbol cache (see
        news_sentiment.py) via the shared instance strategy_manager.py
        already creates, so this costs nothing extra beyond what the old
        standalone sentiment strategy already did.
        """
        analyzer = getattr(self.strategy, "news_sentiment", None)
        if analyzer is None:
            return []
        candidates = []
        self._candidate_scores = {}
        for symbol in config.NEWS_SENTIMENT_UNIVERSE:
            if symbol in self.symbols:
                continue
            score = analyzer._get_sentiment_score(symbol)
            if score > config.SCALPING_SENTIMENT_RELEVANCE_THRESHOLD:
                candidates.append(symbol)
                self._candidate_scores[symbol] = score
        return candidates

    def get_exit_levels(self, symbol):
        """(stop_price, target_price) for symbol's most recent rider-sourced
        BUY signal, or None if it didn't fire one this cycle (including any
        base-universe BUY — those fall back to the uniform per-strategy
        risk% via strategy_manager, unchanged)."""
        return self.exit_levels.get(symbol)

    def get_signal_reason(self, symbol):
        """Human-readable reason for this cycle's BUY on symbol, if it was
        sourced from the news-sentiment rider (see _sentiment_candidates).
        None for a base-universe BUY — strategy_manager falls back to its
        own generic reason text in that case."""
        return self.rider_reasons.get(symbol)

    def analyze(self):
        """Look for micro price moves in liquid stocks, plus any symbol
        with a current positive news-sentiment spike (see
        _sentiment_candidates) — rider-sourced entries get their own
        tighter exit (see RIDER_STOP_LOSS_PERCENT / get_exit_levels)."""
        decisions = {}
        self.exit_levels = {}
        self.rider_reasons = {}
        # Backtests (YahooDataBacktesting) have no real minute data — worse,
        # confirmed 2026-09-01: Lumibot's YahooData._data_store caches a
        # fetched dataframe by ASSET ONLY, not (asset, timestep). Once any
        # other strategy fetches a symbol's DAILY bars in the same backtest,
        # a later "minute" request for that same symbol silently gets the
        # cached DAILY data back instead — mislabeled, but shaped like
        # enough real bars (2 rows) to pass the check below and fire on
        # daily volatility, not real intraday moves. This produced
        # thousands of fabricated signals (every one landing at exactly
        # 09:30:00, the single daily backtest iteration time) before this
        # was caught — see project memory. Scalping can only be
        # meaningfully tested against real minute data (e.g. directly via
        # Alpaca's historical data API, bypassing Lumibot's backtest data
        # source entirely, as sandbox_scalping_candidates.py does), never
        # mixed into the same YahooDataBacktesting run as the other 7
        # daily-bar strategies.
        if getattr(self.strategy, "is_backtesting", False):
            return decisions
        held_symbols = [p.symbol for p in self.strategy.get_positions()]
        routed = regime_router.get_symbols_for(self.strategy, "scalping")
        self.symbols = list(dict.fromkeys(routed + held_symbols))
        rider_symbols = set(self._sentiment_candidates())
        scan_symbols = list(self.symbols) + list(rider_symbols)
        for symbol in scan_symbols:
            price = self.strategy.get_last_price(symbol)
            if not price:
                continue
            # Example: scalp if price moved >0.2% in last minute.
            # length must be >=2 — the code below compares the last two
            # bars, but Lumibot's data source always trims the result down
            # to exactly `length` rows (`if len(df) > length: df =
            # df.iloc[-length:]`), so length=1 could NEVER return 2 rows —
            # this strategy was structurally incapable of ever firing a
            # signal until this was fixed 2026-08-31.
            bars = self.strategy.get_historical_prices(symbol, 2, "minute")
            if bars and len(bars.df) >= 2:
                change = (bars.df["close"].iloc[-1] - bars.df["close"].iloc[-2]) / bars.df["close"].iloc[-2]
                if change > 0.002:
                    decisions[symbol] = "BUY"
                    if symbol in rider_symbols:
                        self.exit_levels[symbol] = (
                            price * (1 - RIDER_STOP_LOSS_PERCENT),
                            price * (1 + RIDER_TAKE_PROFIT_PERCENT),
                        )
                        score = getattr(self, "_candidate_scores", {}).get(symbol)
                        self.rider_reasons[symbol] = (
                            f"News sentiment spike (score {score:.2f}) + 0.2% price move"
                            if score is not None else "News sentiment spike + 0.2% price move"
                        )
                    else:
                        # Same uniform per-strategy risk% as a base-universe
                        # entry (no exit_levels override) — this reacts to
                        # the same kind of real-time price tick, from a
                        # regime-routed, low-cost-screened watchlist,
                        # unlike the sentiment rider's possibly-delayed
                        # news trigger.
                        self.rider_reasons[symbol] = "Regime-routed, low-cost screen + 0.2% price move"
                elif change < -0.002:
                    decisions[symbol] = "SELL"
        return decisions
