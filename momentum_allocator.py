"""Momentum-based stock allocator strategy.

Dynamically allocates capital to top-performing momentum stocks.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd

import config

logger = logging.getLogger(__name__)


class MomentumAllocator:
    """Analyzes and allocates capital based on momentum scores."""

    def __init__(self, strategy):
        """Initialize momentum allocator.

        Args:
            strategy: Reference to the main strategy object
        """
        self.strategy = strategy
        self.last_analysis = None
        self.momentum_scores = {}
        self.current_allocations = {}

    def analyze(self) -> Dict[str, str]:
        """Analyze momentum and return trading decisions.

        Returns:
            Dict mapping symbol -> action ("BUY", "SELL", "HOLD")
        """
        try:
            decisions = {}
            lookback_days = config.MOMENTUM_LOOKBACK_DAYS
            top_n = config.MOMENTUM_TOP_N

            # Get historical prices for momentum calculation
            scores = self._calculate_momentum_scores(
                config.MOMENTUM_UNIVERSE, lookback_days
            )

            if not scores:
                logger.warning("No momentum scores calculated")
                return decisions

            self.momentum_scores = scores

            # Get top performers
            top_performers = sorted(
                scores.items(), key=lambda x: x[1], reverse=True
            )[:top_n]

            logger.info(f"Top momentum performers: {top_performers}")

            # Generate trading decisions
            for symbol, score in top_performers:
                position = self.strategy.get_position(symbol)

                if score > 0.7:  # Strong uptrend
                    if not position or position.quantity < config.MAX_POSITION_SIZE:
                        decisions[symbol] = "BUY"
                elif score < 0.3:  # Downtrend
                    if position:
                        decisions[symbol] = "SELL"
                else:  # Neutral
                    decisions[symbol] = "HOLD"

            # Close positions not in top performers
            for pos in self.strategy.get_positions():
                if pos.symbol not in [s[0] for s in top_performers]:
                    decisions[pos.symbol] = "SELL"

            self.last_analysis = datetime.now()
            return decisions

        except Exception as e:
            logger.error(f"Error in momentum analysis: {e}", exc_info=True)
            return {}

    def _calculate_momentum_scores(self, universe: List[str], lookback_days: int) -> Dict[str, float]:
        """Calculate momentum scores for a stock universe.

        Momentum Score = (Price_Today - Price_N_Days_Ago) / Price_N_Days_Ago

        Args:
            universe: List of stock symbols
            lookback_days: Number of days to look back

        Returns:
            Dict mapping symbol -> momentum score (0-1)
        """
        scores = {}

        for symbol in universe:
            try:
                # Get historical prices
                end_date = datetime.now()
                start_date = end_date - timedelta(days=lookback_days)

                # Get bars from strategy
                bars = self.strategy.get_historical_prices(
                    symbol, lookback_days, "day"
                )

                if not bars or len(bars.df) < 2:
                    logger.warning(f"Insufficient data for {symbol}")
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                past_price = df["close"].iloc[0]

                # Calculate momentum (normalized to 0-1 range)
                momentum = (current_price - past_price) / past_price
                normalized_score = max(0, min(1, momentum + 0.5))  # Scale to 0-1

                scores[symbol] = normalized_score
                logger.debug(f"{symbol}: momentum={momentum:.4f}, score={normalized_score:.4f}")

            except Exception as e:
                logger.error(f"Error calculating momentum for {symbol}: {e}")
                continue

        return scores

    def get_scores(self) -> Dict[str, float]:
        """Get current momentum scores."""
        return self.momentum_scores.copy()
