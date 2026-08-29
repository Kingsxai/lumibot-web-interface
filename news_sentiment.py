"""News sentiment analyzer for trading signals.

Analyzes news sentiment to inform buy/sell decisions using transformer-based NLP.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
import requests
from bs4 import BeautifulSoup
from transformers import pipeline

import config

logger = logging.getLogger(__name__)

# Lazy-loaded sentiment pipeline — do NOT load at import time.
# Loading here would block Flask/SocketIO from ever starting.
_sentiment_pipeline = None


def _get_sentiment_pipeline():
    """Initialize the sentiment-analysis pipeline on first use only."""
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        logger.info("Loading sentiment-analysis pipeline (first use)...")
        _sentiment_pipeline = pipeline("sentiment-analysis")
    return _sentiment_pipeline


def get_yahoo_sentiment(symbol: str) -> float:
    """Fetch and analyze sentiment from Yahoo Finance news.
    
    Args:
        symbol: Stock symbol (e.g., 'AAPL')
    
    Returns:
        Average sentiment score from -1.0 (very negative) to 1.0 (very positive)
    """
    try:
        url = f"https://finance.yahoo.com/quote/{symbol}/news"
        page = requests.get(url, timeout=5)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")

        headlines = [h.get_text() for h in soup.select("h3")]
        scores = []

        sentiment_pipeline = _get_sentiment_pipeline()
        for h in headlines[:5]:  # Analyze top 5 headlines
            result = sentiment_pipeline(h)[0]
            if result["label"] == "POSITIVE":
                scores.append(result["score"])
            elif result["label"] == "NEGATIVE":
                scores.append(-result["score"])
        
        return sum(scores) / len(scores) if scores else 0.0
    
    except Exception as e:
        logger.error(f"Error fetching Yahoo sentiment for {symbol}: {e}")
        return 0.0


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

                if sentiment > config.SENTIMENT_BUY_THRESHOLD:
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

            # Fetch sentiment from Yahoo Finance
            score = get_yahoo_sentiment(symbol)
            self.news_cache[symbol] = (datetime.now(), score)
            return score

        except Exception as e:
            logger.error(f"Error getting sentiment for {symbol}: {e}")
            return 0.0

    def get_scores(self) -> Dict[str, float]:
        """Get current sentiment scores."""
        return self.sentiment_scores.copy()
