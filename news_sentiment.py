"""News sentiment analyzer — not its own trading strategy (2026-09-01, see
project memory project-sentiment-folded-into-scalping); scores news to tell
scalping.py which symbols are currently "relevant" enough to add as scan
candidates (see scalping.py's _sentiment_candidates).

Uses Alpaca's own News API (alpaca.data.historical.news.NewsClient) — no new
API key needed, already have Alpaca credentials. Replaced Yahoo Finance
scraping 2026-09-01: Yahoo required a spoofed User-Agent to avoid 429s and
had no historical depth; Alpaca's feed is authenticated, real, and confirmed
(backtest_scalping_sentiment_historical.py) to have deep historical coverage
too, so the exact same aggregation logic works for both live and backtest.

IMPORTANT — assumed delayed, not real-time: this account's Alpaca tier gets
whatever news latency comes with it; low-latency/real-time news is a paid
add-on not purchased yet (user's call, "we will be implementing later"). A
symbol scalping enters via the sentiment rider may already be past the price
move the news caused by the time this fires — that's a materially different
risk than a base-universe entry reacting to real-time price action, which is
why scalping.py gives rider-sourced entries their own tighter exit levels
(see scalping.py's get_exit_levels / RIDER_STOP_LOSS_PERCENT).
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional
from transformers import pipeline

import config

logger = logging.getLogger(__name__)

# Lazy-loaded sentiment pipeline — do NOT load at import time.
# Loading here would block Flask/SocketIO from ever starting.
_sentiment_pipeline = None

# 2026-09-02: was the default distilbert/sst-2 model — a GENERIC (movie-
# review-trained) binary classifier with no concept of financial language
# and no neutral class. Confirmed live: it misread "Rosenblatt Maintains
# Neutral on Apple, Raises Price Target to $303" (bullish) and "CNBC
# Halftime Report Final Trades: ..." (a TV segment name, not sentiment at
# all) as NEGATIVE with >95% confidence — and did this systematically
# enough that every one of the 10 scalping-rider universe symbols showed
# a strongly negative score simultaneously, making the rider's +0.6
# positive threshold effectively unreachable and explaining why it had
# never fired once in 184 logged scalping signals. FinBERT is actually
# trained on financial text with a real positive/negative/NEUTRAL output
# — reclassified both examples above correctly (neutral) in live
# testing, plus clean positive/negative control headlines. Affects both
# the scalping rider AND the emergency stop's liquidation trigger, not
# just display — user confirmed before this was made live.
_SENTIMENT_MODEL = "ProsusAI/finbert"
# FinBERT's labels are lowercase; "neutral" contributes 0 (no directional
# signal) rather than being forced into positive/negative like the old
# binary model did.
_LABEL_SIGN = {"positive": 1, "negative": -1, "neutral": 0}


def _get_sentiment_pipeline():
    """Initialize the sentiment-analysis pipeline on first use only."""
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        logger.info(f"Loading sentiment-analysis pipeline ({_SENTIMENT_MODEL}, first use)...")
        _sentiment_pipeline = pipeline("sentiment-analysis", model=_SENTIMENT_MODEL)
    return _sentiment_pipeline


def _headlines_to_score(headlines) -> tuple:
    """Shared scoring aggregation for both get_alpaca_sentiment and
    get_yahoo_sentiment — averages every headline's signed score
    (positive/negative/neutral, see _LABEL_SIGN), not just a handful of
    recent ones, matching backtest_scalping_sentiment_historical.py's
    aggregation so config.SCALPING_SENTIMENT_RELEVANCE_THRESHOLD stays
    calibrated the same way live and in backtest.

    Returns (score, article_count) — score averages -1.0 (very negative)
    to 1.0 (very positive); article_count is how many headlines that
    average is over, including neutral ones (they dilute the average
    toward 0 rather than being excluded, which is the correct behavior
    for "mixed/inconclusive evidence").
    """
    if not headlines:
        return 0.0, 0
    sentiment_pipeline = _get_sentiment_pipeline()
    results = sentiment_pipeline(headlines, truncation=True)
    scores = [_LABEL_SIGN.get(r["label"].lower(), 0) * r["score"] for r in results]
    score = sum(scores) / len(scores) if scores else 0.0
    return score, len(headlines)


def get_alpaca_sentiment(symbol: str, lookback_hours: float = None) -> tuple:
    """Fetch and analyze sentiment from Alpaca's News API, averaged over
    EVERY headline in the lookback window — not just a handful of recent
    ones — so this means exactly what
    backtest_scalping_sentiment_historical.py's historical aggregation
    means, and config.SCALPING_SENTIMENT_RELEVANCE_THRESHOLD is calibrated
    consistently in both places.

    Args:
        symbol: Stock symbol (e.g., 'AAPL')
        lookback_hours: how far back to look; defaults to
            config.NEWS_LOOKBACK_HOURS (the scalping-rider relevance-check
            window). The emergency stop passes its own, shorter
            config.NEWS_EMERGENCY_STOP_LOOKBACK_HOURS instead — it wants to
            react to what's fresh, not something already priced in hours ago.

    Returns:
        (score, article_count) — score averages -1.0 (very negative) to 1.0
        (very positive); article_count is how many headlines that average
        is over. Confirmed live 2026-09-01: with very few articles, the
        average can swing to an extreme off headlines that aren't even
        primarily ABOUT this symbol — Alpaca's `symbols` field tags any
        article that MENTIONS a symbol (e.g. a competitor-comparison
        piece), not just ones where it's the actual subject. Callers that
        act on a small article count (see _get_emergency_sentiment_score)
        should treat it as insufficient evidence, not a real signal.
    """
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"])
        hours = lookback_hours if lookback_hours is not None else config.NEWS_LOOKBACK_HOURS
        start = datetime.now() - timedelta(hours=hours)
        req = NewsRequest(symbols=symbol, start=start, limit=1000)
        result = client.get_news(req)
        headlines = [a.headline for a in result.data["news"]]
        return _headlines_to_score(headlines)

    except Exception as e:
        logger.error(f"Error fetching Alpaca sentiment for {symbol}: {e}")
        return 0.0, 0


# Yahoo requires an exchange suffix for non-US-primary listings — a bare
# IB ticker (e.g. "ANTO", what strategy_manager.py's positions carry)
# resolves to NOTHING on Yahoo ("Quote not found", confirmed live
# 2026-09-02), only "ANTO.L" does. Built from config.INTERNATIONAL_
# MARKETS' exchange field rather than hardcoded per-symbol, so it stays
# correct if that universe changes.
_YAHOO_EXCHANGE_SUFFIX = {"LSE": ".L", "ASX": ".AX"}


def _to_yahoo_symbol(symbol: str) -> str:
    for market_cfg in config.INTERNATIONAL_MARKETS.values():
        if symbol in market_cfg["universe"]:
            suffix = _YAHOO_EXCHANGE_SUFFIX.get(market_cfg["exchange"])
            if suffix:
                return f"{symbol}{suffix}"
    return symbol  # US (or already-unrecognized) symbol — used as-is


def get_yahoo_sentiment(symbol: str, lookback_hours: float) -> tuple:
    """Fetch and analyze sentiment from Yahoo Finance (via yfinance) —
    fallback for symbols Alpaca's News API has no coverage for at all
    (confirmed live 2026-09-02: Alpaca AND this account's free IB news
    providers both return zero articles for LSE/ASX names like ANTO —
    a genuine account-tier limitation, not a bug; see
    project_ib_news_coverage_gap memory). yfinance's .news (Yahoo's own
    JSON search API under the hood, not HTML scraping) returned real,
    on-topic articles for ANTO.L in live testing — unlike the OLD dead
    sentiment.py's raw HTML scrape of finance.yahoo.com/quote/.../news,
    which is both rate-limited (429 without a spoofed User-Agent) AND
    now 404s outright (Yahoo's page structure has changed since that was
    written) — this uses a different, still-working endpoint.

    Only called as a fallback when the primary source found nothing, so
    the "no historical depth" reason Yahoo was dropped for the scalping-
    rider/backtest use case (see module docstring) doesn't apply here —
    this only ever needs to see recent news, never deep history.

    Same (score, article_count) shape as get_alpaca_sentiment so callers
    don't need to know which source actually answered.
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(_to_yahoo_symbol(symbol))
        news = ticker.news or []
        cutoff = datetime.now(tz=None) - timedelta(hours=lookback_hours)
        headlines = []
        for item in news:
            content = item.get("content", item)
            title = content.get("title")
            pub_date = content.get("pubDate")
            if not title or not pub_date:
                continue
            try:
                published = datetime.fromisoformat(pub_date.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                continue
            if published >= cutoff:
                headlines.append(title)

        return _headlines_to_score(headlines)

    except Exception as e:
        logger.error(f"Error fetching Yahoo sentiment for {symbol}: {e}")
        return 0.0, 0


class NewsSentimentAnalyzer:
    """Analyzes news sentiment for trading decisions using transformer-based NLP."""

    def __init__(self, strategy):
        """Initialize news sentiment analyzer.

        Args:
            strategy: Reference to the main strategy object
        """
        self.strategy = strategy
        self.last_analysis = None
        self.sentiment_scores = {}
        self.news_cache = {}
        # Separate cache for the emergency-stop check (strategy_manager.py's
        # _check_news_emergency_stop) — deliberately its own dict with a
        # shorter TTL matching config.NEWS_EMERGENCY_STOP_LOOKBACK_HOURS's
        # window, since reusing news_cache (1h TTL, tied to the 24h
        # relevance-check window) could serve a stale reading for a check
        # that specifically wants to react to what's fresh.
        self.emergency_cache = {}

    def analyze(self) -> Dict[str, str]:
        """Analyze news sentiment and return trading decisions.

        Returns:
            Dict mapping symbol -> action ("BUY", "SELL", "HOLD")
        """
        try:
            decisions = {}

            # Fetch and analyze news for each stock
            for symbol in config.NEWS_SENTIMENT_UNIVERSE:
                sentiment = self._get_sentiment_score(symbol)
                self.sentiment_scores[symbol] = sentiment

                position = self.strategy.get_position(symbol)

                if sentiment > config.SCALPING_SENTIMENT_RELEVANCE_THRESHOLD:
                    if not position:
                        decisions[symbol] = "BUY"
                elif sentiment < config.SENTIMENT_SELL_THRESHOLD:
                    if position:
                        decisions[symbol] = "SELL"
                else:
                    decisions[symbol] = "HOLD"

            self.last_analysis = datetime.now()
            return decisions

        except Exception as e:
            logger.error(f"Error in sentiment analysis: {e}", exc_info=True)
            return {}

    def _get_sentiment_score(self, symbol: str) -> float:
        """Get sentiment score for a symbol.

        Args:
            symbol: Stock symbol

        Returns:
            Sentiment score from -1.0 (very negative) to 1.0 (very positive)
        """
        try:
            # Check cache first
            if symbol in self.news_cache:
                cache_time, score = self.news_cache[symbol]
                if datetime.now() - cache_time < timedelta(hours=1):
                    return score

            # Fetch sentiment from Alpaca's News API
            score, _article_count = get_alpaca_sentiment(symbol)
            self.news_cache[symbol] = (datetime.now(), score)
            return score

        except Exception as e:
            logger.error(f"Error getting sentiment for {symbol}: {e}")
            return 0.0

    def _get_emergency_sentiment_score(self, symbol: str) -> float:
        """Sentiment score for the news emergency stop (strategy_manager.py's
        _check_news_emergency_stop) — its own short-window fetch and its own
        cache (see emergency_cache in __init__), separate from
        _get_sentiment_score's 24h-window/1h-cache used for the scalping
        rider relevance check.

        Fixed 2026-09-01, live: fired on TSLA off just 3 articles, none of
        them genuinely severe TSLA-specific news (a NIO-outlook piece, a
        "competitor beating Tesla" comparison, and a mixed/ambiguous quote
        a plain classifier misread as strongly negative) — averaging that
        few headlines is too noisy for a mechanism that triggers a real
        forced liquidation. Now requires
        config.NEWS_EMERGENCY_STOP_MIN_ARTICLES headlines before treating
        the score as real evidence; below that, returns neutral (0.0) —
        insufficient evidence to liquidate, not a signal either way.

        2026-09-02: falls back to Yahoo Finance (get_yahoo_sentiment) when
        Alpaca finds zero articles — confirmed live that Alpaca's News API
        has no coverage at all for international (LSE/ASX) symbols, which
        made this check silently inert for every non-US position (see
        project_ib_news_coverage_gap memory). Alpaca stays primary since
        it's the better-covered, already-calibrated source for US names;
        Yahoo only fills the specific gap Alpaca can't.
        """
        try:
            if symbol in self.emergency_cache:
                cache_time, score = self.emergency_cache[symbol]
                if datetime.now() - cache_time < timedelta(minutes=15):
                    return score

            score, article_count = get_alpaca_sentiment(
                symbol, lookback_hours=config.NEWS_EMERGENCY_STOP_LOOKBACK_HOURS
            )
            if article_count == 0:
                score, article_count = get_yahoo_sentiment(
                    symbol, lookback_hours=config.NEWS_EMERGENCY_STOP_LOOKBACK_HOURS
                )
            if article_count < config.NEWS_EMERGENCY_STOP_MIN_ARTICLES:
                logger.info(
                    f"Emergency sentiment for {symbol}: only {article_count} article(s) "
                    f"(< {config.NEWS_EMERGENCY_STOP_MIN_ARTICLES}), treating as neutral"
                )
                score = 0.0
            self.emergency_cache[symbol] = (datetime.now(), score)
            return score

        except Exception as e:
            logger.error(f"Error getting emergency sentiment for {symbol}: {e}")
            return 0.0

    def get_scores(self) -> Dict[str, float]:
        """Get current sentiment scores."""
        return self.sentiment_scores.copy()
