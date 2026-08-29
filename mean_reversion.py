import logging
logger = logging.getLogger(__name__)

class MeanReversionStrategy:
    def __init__(self, strategy):
        self.strategy = strategy

    def analyze(self):
        """Trade RSI extremes expecting reversion."""
        decisions = {}
        for symbol in ["AAPL", "MSFT", "NVDA"]:
            bars = self.strategy.get_historical_prices(symbol, 14, "day")
            if not bars or len(bars.df) < 14:
                continue
            df = bars.df
            delta = df["close"].diff()
            gain = delta.clip(lower=0).mean()
            loss = -delta.clip(upper=0).mean()
            rs = gain / loss if loss != 0 else 0
            rsi = 100 - (100 / (1 + rs))
            if rsi < 30:
                decisions[symbol] = "BUY"
            elif rsi > 70:
                decisions[symbol] = "SELL"
        return decisions
