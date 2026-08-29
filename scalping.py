import logging
logger = logging.getLogger(__name__)

class ScalpingStrategy:
    def __init__(self, strategy):
        self.strategy = strategy

    def analyze(self):
        """Look for micro price moves in liquid stocks."""
        decisions = {}
        for symbol in ["AAPL", "MSFT", "AMZN"]:
            price = self.strategy.get_last_price(symbol)
            if not price:
                continue
            # Example: scalp if price moved >0.2% in last minute
            bars = self.strategy.get_historical_prices(symbol, 1, "minute")
            if bars and len(bars.df) >= 2:
                change = (bars.df["close"].iloc[-1] - bars.df["close"].iloc[-2]) / bars.df["close"].iloc[-2]
                if change > 0.002:
                    decisions[symbol] = "BUY"
                elif change < -0.002:
                    decisions[symbol] = "SELL"
        return decisions
