"""Run the MultiStrategyBot backtest across several symbols and time periods,
accumulating signals into the same signals.db, to get a broader, more
reliable read on strategy and confirmation-layer quality than any single
symbol/period can give.

Usage:
    python run_multi_backtest.py
"""

import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Symbols to scope momentum/sentiment toward across runs. The other 6
# strategies trade their own hardcoded universes regardless of this value,
# so this mainly diversifies momentum/sentiment coverage.
SYMBOLS = ["AAPL", "MSFT", "TSLA"]

# Distinct date windows spanning different market regimes, so results
# aren't overfit to one narrow stretch of price action.
PERIODS = [
    ("2022-01-01", "2022-06-30"),  # 2022 downturn / high volatility
    ("2022-07-01", "2022-12-31"),  # 2022 downturn, second half
    ("2023-01-01", "2023-06-30"),  # 2023 recovery
    ("2023-07-01", "2023-12-31"),  # 2023 recovery, second half
    ("2024-01-01", "2024-06-30"),  # 2024 (already tested individually)
    ("2024-07-01", "2024-12-31"),  # 2024, second half
]


def run_one(symbol: str, start: str, end: str):
    import config
    config.MOMENTUM_UNIVERSE = [symbol]
    config.NEWS_SENTIMENT_UNIVERSE = [symbol]

    from lumibot.backtesting import YahooDataBacktesting
    from lumibot.entities import Asset
    from strategy_manager import MultiStrategyBot

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    benchmark_asset = Asset(symbol=symbol, asset_type="stock")

    logger.info(f"=== Running backtest: symbol={symbol}, {start} to {end} ===")
    try:
        MultiStrategyBot.backtest(
            datasource_class=YahooDataBacktesting,
            backtesting_start=start_dt,
            backtesting_end=end_dt,
            benchmark_asset=benchmark_asset,
            show_plot=False,
            show_tearsheet=False,
            save_tearsheet=False,
        )
        logger.info(f"=== Completed: symbol={symbol}, {start} to {end} ===")
    except Exception as e:
        logger.error(f"=== Failed: symbol={symbol}, {start} to {end}: {e} ===", exc_info=True)


def main():
    total_runs = len(SYMBOLS) * len(PERIODS)
    run_num = 0
    for symbol in SYMBOLS:
        for start, end in PERIODS:
            run_num += 1
            logger.info(f"--- Run {run_num}/{total_runs} ---")
            run_one(symbol, start, end)

    logger.info("All backtest runs complete. Run label_signals.py next.")


if __name__ == "__main__":
    main()
