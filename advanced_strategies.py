"""Advanced Trading Strategies for Lumibot.

Includes:
- Scalping Strategy
- Breakout Trading
- Mean Reversion
- VWAP Strategy
- Gap-and-Go
- Reversal Trading

Each strategy can trigger with partial conditions and logs all trades.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np

import config

logger = logging.getLogger(__name__)


class TradeLogger:
    """Logs all trades for analysis and performance tracking."""

    def __init__(self):
        self.trades = []

    def log_trade(self, symbol: str, strategy: str, side: str, quantity: int, 
                  entry_price: float, reason: str, conditions_met: Dict = None):
        """Log a trade execution."""
        trade = {
            "timestamp": datetime.now(),
            "symbol": symbol,
            "strategy": strategy,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "reason": reason,
            "conditions_met": conditions_met or {},
        }
        self.trades.append(trade)
        logger.info(
            f"✓ TRADE: {strategy} - {side.upper()} {quantity}x {symbol} @ ${entry_price:.2f} | {reason}"
        )

    def log_failed_condition(self, symbol: str, strategy: str, reason: str):
        """Log when trade conditions weren't met."""
        logger.debug(f"✗ {strategy} ({symbol}): {reason}")

    def get_summary(self) -> Dict:
        """Get trading summary."""
        if not self.trades:
            return {}
        
        df = pd.DataFrame(self.trades)
        return {
            "total_trades": len(df),
            "buy_trades": len(df[df["side"] == "buy"]),
            "sell_trades": len(df[df["side"] == "sell"]),
            "strategies_used": df["strategy"].unique().tolist(),
            "symbols_traded": df["symbol"].unique().tolist(),
        }


# Global trade logger
trade_logger = TradeLogger()


class ScalpingStrategy:
    """Scalping: Quick trades on small price movements (0.5-2% targets)."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "Scalping"

    def analyze(self) -> Dict[str, str]:
        """Analyze for scalping opportunities."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE[:5]:  # Focus on top 5
            try:
                # Get 1-minute bars (or lowest available)
                bars = self.strategy.get_historical_prices(symbol, 5, "minute")
                if not bars or len(bars.df) < 3:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                prev_price = df["close"].iloc[-2]
                high = df["high"].iloc[-1]
                low = df["low"].iloc[-1]

                # Condition: Price momentum in last minute
                momentum = (current_price - prev_price) / prev_price
                volatility = (high - low) / low

                conditions = {
                    "momentum": momentum,
                    "volatility": volatility,
                    "price": current_price,
                }

                # Buy if: Strong upward momentum AND low volatility (controlled)
                if momentum > 0.003 and volatility < 0.02:  # 0.3% momentum, <2% range
                    if not self.strategy.get_position(symbol):
                        decisions[symbol] = "BUY"
                        trade_logger.log_trade(
                            symbol, self.name, "buy", 10, current_price,
                            f"Strong momentum: {momentum*100:.2f}%",
                            conditions
                        )
                    else:
                        trade_logger.log_failed_condition(symbol, self.name, "Already holding")

                # Sell if: Downward momentum
                elif momentum < -0.002:
                    position = self.strategy.get_position(symbol)
                    if position:
                        decisions[symbol] = "SELL"
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Momentum reversal: {momentum*100:.2f}%",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class BreakoutStrategy:
    """Breakout Trading: Trade when price breaks resistance/support levels."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "Breakout"

    def analyze(self) -> Dict[str, str]:
        """Analyze for breakout opportunities."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE:
            try:
                # Get daily bars for support/resistance
                bars = self.strategy.get_historical_prices(symbol, 20, "day")
                if not bars or len(bars.df) < 5:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                
                # Calculate support and resistance
                resistance = df["high"].tail(10).max()
                support = df["low"].tail(10).min()
                
                # Recent momentum
                momentum = (df["close"].iloc[-1] - df["close"].iloc[-5]) / df["close"].iloc[-5]

                conditions = {
                    "current_price": current_price,
                    "resistance": resistance,
                    "support": support,
                    "momentum": momentum,
                }

                # Buy if: Price breaks above resistance
                if current_price > resistance * 1.001 and momentum > 0.01:
                    if not self.strategy.get_position(symbol):
                        decisions[symbol] = "BUY"
                        trade_logger.log_trade(
                            symbol, self.name, "buy", 5, current_price,
                            f"Breakout above ${resistance:.2f}",
                            conditions
                        )

                # Sell if: Price breaks below support
                elif current_price < support * 0.999:
                    position = self.strategy.get_position(symbol)
                    if position:
                        decisions[symbol] = "SELL"
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Breakdown below ${support:.2f}",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class MeanReversionStrategy:
    """Mean Reversion: Trade when price deviates significantly from average."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "Mean Reversion"

    def analyze(self) -> Dict[str, str]:
        """Analyze for mean reversion opportunities."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE:
            try:
                # Get 20-day bars
                bars = self.strategy.get_historical_prices(symbol, 20, "day")
                if not bars or len(bars.df) < 10:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                
                # Calculate mean and standard deviation
                mean_price = df["close"].mean()
                std_dev = df["close"].std()
                
                # Z-score: how many std devs away from mean
                z_score = (current_price - mean_price) / std_dev if std_dev > 0 else 0

                conditions = {
                    "current_price": current_price,
                    "mean_price": mean_price,
                    "z_score": z_score,
                    "std_dev": std_dev,
                }

                # Buy if: Price is 1.5+ std devs below mean (oversold)
                if z_score < -1.5 and not self.strategy.get_position(symbol):
                    decisions[symbol] = "BUY"
                    trade_logger.log_trade(
                        symbol, self.name, "buy", 8, current_price,
                        f"Oversold: Z-score = {z_score:.2f}",
                        conditions
                    )

                # Sell if: Price is 1.5+ std devs above mean (overbought)
                elif z_score > 1.5:
                    position = self.strategy.get_position(symbol)
                    if position:
                        decisions[symbol] = "SELL"
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Overbought: Z-score = {z_score:.2f}",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class VWAPStrategy:
    """VWAP Strategy: Trade based on Volume-Weighted Average Price."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "VWAP"

    def analyze(self) -> Dict[str, str]:
        """Analyze using VWAP."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE:
            try:
                # Get daily bars with volume
                bars = self.strategy.get_historical_prices(symbol, 20, "day")
                if not bars or len(bars.df) < 5:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                
                # Calculate VWAP
                df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
                df['vwap'] = (df['typical_price'] * df['volume']).cumsum() / df['volume'].cumsum()
                vwap = df['vwap'].iloc[-1]
                
                volume_avg = df['volume'].mean()
                current_volume = df['volume'].iloc[-1]

                conditions = {
                    "current_price": current_price,
                    "vwap": vwap,
                    "volume_ratio": current_volume / volume_avg,
                }

                # Buy if: Price above VWAP + high volume
                if current_price > vwap * 1.002 and current_volume > volume_avg * 1.2:
                    if not self.strategy.get_position(symbol):
                        decisions[symbol] = "BUY"
                        trade_logger.log_trade(
                            symbol, self.name, "buy", 7, current_price,
                            f"Above VWAP (${vwap:.2f}) with high volume",
                            conditions
                        )

                # Sell if: Price below VWAP + declining volume
                elif current_price < vwap * 0.998 and current_volume < volume_avg * 0.8:
                    position = self.strategy.get_position(symbol)
                    if position:
                        decisions[symbol] = "SELL"
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Below VWAP (${vwap:.2f}) with low volume",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class GapAndGoStrategy:
    """Gap-and-Go: Trade stocks that gap up/down at market open."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "Gap-and-Go"
        self.gap_trades = {}

    def analyze(self) -> Dict[str, str]:
        """Analyze for gap opportunities."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE:
            try:
                # Get recent bars
                bars = self.strategy.get_historical_prices(symbol, 5, "day")
                if not bars or len(bars.df) < 2:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                prev_close = df["close"].iloc[-2]
                open_price = df["open"].iloc[-1]
                
                # Calculate gap
                gap = (open_price - prev_close) / prev_close
                gap_percent = gap * 100

                conditions = {
                    "current_price": current_price,
                    "prev_close": prev_close,
                    "open_price": open_price,
                    "gap_percent": gap_percent,
                }

                # Buy if: Gap up > 1% and price continues up
                if gap > 0.01 and current_price > open_price:
                    if symbol not in self.gap_trades and not self.strategy.get_position(symbol):
                        decisions[symbol] = "BUY"
                        self.gap_trades[symbol] = True
                        trade_logger.log_trade(
                            symbol, self.name, "buy", 6, current_price,
                            f"Gap up {gap_percent:.2f}% holding above open",
                            conditions
                        )

                # Sell if: Gap down > 1%
                elif gap < -0.01:
                    position = self.strategy.get_position(symbol)
                    if position and symbol in self.gap_trades:
                        decisions[symbol] = "SELL"
                        del self.gap_trades[symbol]
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Close gap-down trade",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class ReversalStrategy:
    """Reversal Trading: Trade at support/resistance reversals."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.name = "Reversal"

    def analyze(self) -> Dict[str, str]:
        """Analyze for reversal opportunities."""
        decisions = {}

        for symbol in config.MOMENTUM_UNIVERSE:
            try:
                # Get bars
                bars = self.strategy.get_historical_prices(symbol, 10, "day")
                if not bars or len(bars.df) < 5:
                    continue

                df = bars.df
                current_price = df["close"].iloc[-1]
                
                # Find pivot points
                high = df["high"].tail(5).max()
                low = df["low"].tail(5).min()
                pivot = (high + low + df["close"].iloc[-1]) / 3
                
                # Identify reversal patterns
                prev_close = df["close"].iloc[-2]
                prev_open = df["open"].iloc[-2]
                
                # Hammer/Inverted hammer pattern
                body = abs(prev_close - prev_open)
                wick = prev_close - prev_open
                
                conditions = {
                    "current_price": current_price,
                    "pivot": pivot,
                    "high": high,
                    "low": low,
                    "body_size": body,
                }

                # Buy if: Price near support and starts reversing up
                if current_price < pivot and current_price > low * 1.01:
                    if not self.strategy.get_position(symbol):
                        decisions[symbol] = "BUY"
                        trade_logger.log_trade(
                            symbol, self.name, "buy", 9, current_price,
                            f"Support reversal at pivot ${pivot:.2f}",
                            conditions
                        )

                # Sell if: Price near resistance and starts reversing down
                elif current_price > pivot and current_price < high * 0.99:
                    position = self.strategy.get_position(symbol)
                    if position:
                        decisions[symbol] = "SELL"
                        trade_logger.log_trade(
                            symbol, self.name, "sell", position.quantity, current_price,
                            f"Resistance reversal at pivot ${pivot:.2f}",
                            conditions
                        )

            except Exception as e:
                trade_logger.log_failed_condition(symbol, self.name, str(e))

        return decisions


class AdvancedStrategiesManager:
    """Manages all advanced trading strategies."""

    def __init__(self, strategy_obj):
        self.strategy = strategy_obj
        self.scalping = ScalpingStrategy(strategy_obj)
        self.breakout = BreakoutStrategy(strategy_obj)
        self.mean_reversion = MeanReversionStrategy(strategy_obj)
        self.vwap = VWAPStrategy(strategy_obj)
        self.gap_and_go = GapAndGoStrategy(strategy_obj)
        self.reversal = ReversalStrategy(strategy_obj)

    def analyze_all(self) -> Dict[str, str]:
        """Run all strategies and combine signals."""
        all_decisions = {}

        strategies = [
            self.scalping,
            self.breakout,
            self.mean_reversion,
            self.vwap,
            self.gap_and_go,
            self.reversal,
        ]

        for strategy in strategies:
            try:
                decisions = strategy.analyze()
                all_decisions.update(decisions)
            except Exception as e:
                logger.error(f"Error in {strategy.name}: {e}", exc_info=True)

        return all_decisions

    def get_trade_summary(self) -> Dict:
        """Get summary of all trades executed."""
        return trade_logger.get_summary()
