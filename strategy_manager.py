"""Multi-strategy manager for Lumibot trading bot.

Manages:
- Momentum Allocator Strategy
- News Sentiment Strategy
- Scalping Strategy
- Breakout Strategy
- Mean Reversion Strategy
- VWAP Strategy
- Gap and Go Strategy
- Reversal Strategy
- Bracket Orders (stop-loss + take-profit)
- Position lifecycle and graceful shutdown
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from decimal import Decimal
import threading
import time

from lumibot.strategies import Strategy
from lumibot.entities import Asset, Order
import pandas as pd

import config
from news_sentiment import NewsSentimentAnalyzer
from momentum_allocator import MomentumAllocator
from scalping import ScalpingStrategy
from breakout import BreakoutStrategy
from mean_reversion import MeanReversionStrategy
from vwap import VWAPStrategy
from gap_and_go import GapAndGoStrategy
from reversal import ReversalStrategy

logger = logging.getLogger(__name__)


class BracketOrder:
    """Represents a bracket order (entry + stop-loss + take-profit)."""

    def __init__(self, symbol: str, entry_price: float, quantity: int):
        self.symbol = symbol
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_time = datetime.now()
        self.stop_loss_price = entry_price * (1 - config.STOP_LOSS_PERCENT)
        self.take_profit_price = entry_price * (1 + config.TAKE_PROFIT_PERCENT)
        self.entry_order_id = None
        self.stop_loss_order_id = None
        self.take_profit_order_id = None
        self.status = "PENDING"  # PENDING, FILLED, CLOSED

    def __repr__(self):
        return f"BracketOrder({self.symbol} x{self.quantity} @ ${self.entry_price:.2f})"


class MultiStrategyBot(Strategy):
    """Main strategy class that coordinates multiple trading strategies."""

    def initialize(self):
        """Initialize the multi-strategy bot."""
        self.sleeptime = 60  # Check every minute

        # Strategy states
        self.is_trading = False
        self.shutdown_requested = False

        # Initialize sub-strategies
        self.momentum_allocator = MomentumAllocator(self)
        self.news_sentiment = NewsSentimentAnalyzer(self)
        self.scalping = ScalpingStrategy(self)
        self.breakout = BreakoutStrategy(self)
        self.mean_reversion = MeanReversionStrategy(self)
        self.vwap = VWAPStrategy(self)
        self.gap_and_go = GapAndGoStrategy(self)
        self.reversal = ReversalStrategy(self)

        # Track bracket orders
        self.bracket_orders: Dict[str, BracketOrder] = {}
        self.closed_positions: List[Dict] = []

        # Performance tracking
        self.trade_history = []
        self.strategy_performance = {
            "momentum": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "sentiment": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
        }
        self.strategy_performance.update({
            "scalping": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "breakout": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "mean_reversion": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "vwap": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "gap_and_go": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
            "reversal": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0},
        })

        logger.info("MultiStrategyBot initialized")

    def on_trading_iteration(self):
        """Main trading loop executed every sleeptime interval."""
        try:
            # Check for graceful shutdown
            if self.shutdown_requested:
                self._liquidate_all_positions()
                self.is_trading = False
                logger.info("Bot shutdown complete")
                return

            if not self.is_trading:
                return

            # Update position prices
            self._update_bracket_order_prices()

            # Check if any bracket orders hit stop-loss or take-profit
            self._check_bracket_orders()

            # Run momentum strategy
            self._run_momentum_strategy()

            # Run news sentiment strategy
            self._run_news_sentiment_strategy()

            # Run additional strategies
            self._run_strategy("scalping", self.scalping.analyze())
            self._run_strategy("breakout", self.breakout.analyze())
            self._run_strategy("mean_reversion", self.mean_reversion.analyze())
            self._run_strategy("vwap", self.vwap.analyze())
            self._run_strategy("gap_and_go", self.gap_and_go.analyze())
            self._run_strategy("reversal", self.reversal.analyze())

            # Rebalance portfolio
            self._rebalance_portfolio()

            logger.debug("Trading iteration complete")

        except Exception as e:
            logger.error(f"Error in trading iteration: {e}", exc_info=True)

    def _run_momentum_strategy(self):
        """Execute momentum allocator strategy."""
        try:
            decisions = self.momentum_allocator.analyze()
            if not decisions:
                return

            available_capital = self._get_available_capital(
                config.MOMENTUM_ALLOCATION_PERCENT
            )

            for symbol, action in decisions.items():
                if action == "BUY":
                    self._place_bracket_order(symbol, "buy", available_capital)
                elif action == "SELL":
                    self._close_position(symbol)

        except Exception as e:
            logger.error(f"Error in momentum strategy: {e}", exc_info=True)

    def _run_news_sentiment_strategy(self):
        """Execute news sentiment strategy."""
        try:
            if not config.NEWSAPI_KEY:
                logger.warning("NEWSAPI_KEY not set, skipping sentiment analysis")
                return

            decisions = self.news_sentiment.analyze()
            if not decisions:
                return

            available_capital = self._get_available_capital(
                config.NEWS_ALLOCATION_PERCENT
            )

            for symbol, action in decisions.items():
                if action == "BUY":
                    self._place_bracket_order(symbol, "buy", available_capital)
                elif action == "SELL":
                    self._close_position(symbol)

        except Exception as e:
            logger.error(f"Error in sentiment strategy: {e}", exc_info=True)

    def _run_strategy(self, name, decisions):
        """Execute a generic strategy given its buy/sell decisions.

        Args:
            name: Strategy name (used for logging and performance tracking)
            decisions: Dict mapping symbol -> "BUY"/"SELL" action
        """
        try:
            if not decisions:
                return
            available_capital = self._get_available_capital(config.MAX_POSITION_SIZE)
            for symbol, action in decisions.items():
                if action == "BUY":
                    self._place_bracket_order(symbol, "buy", available_capital)
                    logger.info(f"{name} strategy triggered BUY for {symbol}")
                elif action == "SELL":
                    self._close_position(symbol, reason=name)
                    logger.info(f"{name} strategy triggered SELL for {symbol}")
        except Exception as e:
            logger.error(f"Error in {name} strategy: {e}", exc_info=True)

    def _place_bracket_order(self, symbol: str, side: str, available_capital: float):
        """Place a bracket order (entry + stop-loss + take-profit).

        Args:
            symbol: Stock symbol
            side: "buy" or "sell"
            available_capital: Available capital for this trade
        """
        try:
            # Get current price
            current_price = self.get_last_price(symbol)
            if not current_price:
                logger.warning(f"Could not get price for {symbol}")
                return

            # Calculate position size
            position_size = int(available_capital / current_price)
            if position_size == 0:
                logger.warning(
                    f"Position size for {symbol} is 0, skipping order"
                )
                return

            # Check if we already have a position
            existing_position = self.get_position(symbol)
            if existing_position and side == "buy":
                logger.info(f"Already have position in {symbol}, skipping")
                return

            # Place entry order
            entry_order = self.create_order(symbol, position_size, side)
            self.submit_order(entry_order)
            logger.info(f"Placed {side} order for {position_size}x {symbol} @ ${current_price:.2f}")

            # Create bracket order tracking
            bracket = BracketOrder(symbol, current_price, position_size)
            bracket.entry_order_id = entry_order.id
            self.bracket_orders[symbol] = bracket

        except Exception as e:
            logger.error(f"Error placing bracket order for {symbol}: {e}")

    def _check_bracket_orders(self):
        """Check if any bracket orders have hit stop-loss or take-profit levels."""
        try:
            for symbol, bracket in list(self.bracket_orders.items()):
                current_price = self.get_last_price(symbol)
                if not current_price:
                    continue

                # Check take-profit
                if current_price >= bracket.take_profit_price:
                    logger.info(
                        f"Take-profit hit for {symbol} @ ${current_price:.2f}"
                    )
                    self._close_position(symbol, "take_profit")
                    del self.bracket_orders[symbol]

                # Check stop-loss
                elif current_price <= bracket.stop_loss_price:
                    logger.info(
                        f"Stop-loss hit for {symbol} @ ${current_price:.2f}"
                    )
                    self._close_position(symbol, "stop_loss")
                    del self.bracket_orders[symbol]

        except Exception as e:
            logger.error(f"Error checking bracket orders: {e}")

    def _close_position(self, symbol: str, reason: str = "manual"):
        """Close a position and record trade.

        Args:
            symbol: Stock symbol
            reason: Reason for closing ("take_profit", "stop_loss", "manual", etc)
        """
        try:
            position = self.get_position(symbol)
            if not position:
                logger.warning(f"No position to close for {symbol}")
                return

            # Place sell order
            sell_order = self.create_order(
                symbol, position.quantity, "sell"
            )
            self.submit_order(sell_order)
            logger.info(f"Closed position in {symbol} ({reason})")

            # Record closed position
            self.closed_positions.append({
                "symbol": symbol,
                "quantity": position.quantity,
                "close_reason": reason,
                "close_time": datetime.now(),
            })

        except Exception as e:
            logger.error(f"Error closing position for {symbol}: {e}")

    def _update_bracket_order_prices(self):
        """Update current prices for tracked bracket orders."""
        try:
            symbols = list(self.bracket_orders.keys())
            if not symbols:
                return

            for symbol in symbols:
                bracket = self.bracket_orders[symbol]
                current_price = self.get_last_price(symbol)
                if current_price:
                    bracket.current_price = current_price

        except Exception as e:
            logger.error(f"Error updating bracket prices: {e}")

    def _rebalance_portfolio(self):
        """Rebalance portfolio according to strategy allocations."""
        try:
            portfolio_value = self.get_portfolio_value()
            cash = self.get_cash()
            positions = self.get_positions()

            if not positions:
                return

            # Close positions if portfolio is too leveraged
            if cash < config.MIN_CASH_BUFFER * portfolio_value:
                logger.warning("Cash buffer below minimum, considering liquidation")

        except Exception as e:
            logger.error(f"Error rebalancing portfolio: {e}")

    def _liquidate_all_positions(self):
        """Gracefully liquidate all positions."""
        logger.info("Starting graceful liquidation of all positions...")
        try:
            positions = self.get_positions()
            start_time = time.time()

            # Initialize here so the later check is always valid, even if
            # there were no positions to begin with (previously this could
            # raise UnboundLocalError when `positions` was empty, since the
            # while loop below would never execute).
            remaining_positions = positions

            for position in positions:
                try:
                    sell_order = self.create_order(
                        position.symbol, position.quantity, "sell"
                    )
                    self.submit_order(sell_order)
                    logger.info(f"Liquidated {position.quantity}x {position.symbol}")
                except Exception as e:
                    logger.error(f"Error liquidating {position.symbol}: {e}")

            # Wait for orders to fill (with timeout)
            while time.time() - start_time < config.GRACEFUL_SHUTDOWN_TIMEOUT:
                remaining_positions = self.get_positions()
                if not remaining_positions:
                    logger.info("All positions liquidated")
                    break
                time.sleep(1)

            if remaining_positions:
                logger.warning(
                    f"Timeout: {len(remaining_positions)} positions still open"
                )

        except Exception as e:
            logger.error(f"Error during liquidation: {e}")

    def _get_available_capital(self, allocation_percent: float) -> float:
        """Calculate available capital for a strategy.

        Args:
            allocation_percent: Percentage of portfolio to allocate

        Returns:
            Available capital amount
        """
        try:
            portfolio_value = self.get_portfolio_value()
            cash = self.get_cash()
            min_cash = portfolio_value * config.MIN_CASH_BUFFER

            allocated = portfolio_value * allocation_percent
            available = min(allocated, cash - min_cash)
            return max(0, available)

        except Exception as e:
            logger.error(f"Error calculating available capital: {e}")
            return 0.0

    def start_trading(self):
        """Start the trading bot."""
        self.is_trading = True
        self.shutdown_requested = False
        logger.info("Trading started")

    def stop_trading(self):
        """Stop the trading bot and liquidate positions."""
        self.shutdown_requested = True
        logger.info("Shutdown requested")

    def get_performance_metrics(self) -> Dict:
        """Get performance metrics for both strategies."""
        return {
            "is_trading": self.is_trading,
            "portfolio_value": self.get_portfolio_value(),
            "cash": self.get_cash(),
            "positions": len(self.get_positions()),
            "open_bracket_orders": len(self.bracket_orders),
            "closed_positions": len(self.closed_positions),
            "strategy_performance": self.strategy_performance,
            "timestamp": datetime.now().isoformat(),
        }
