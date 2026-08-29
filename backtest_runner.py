"""Run a Lumibot backtest for MultiStrategyBot, scoped to a single symbol.
Runs as a standalone subprocess so it never touches the live bot's state.

Usage:
    python backtest_runner.py --symbol AAPL --start 2024-01-01 --end 2024-12-31 --output result.json
"""
import argparse
import json
import math
from datetime import datetime


def _make_json_safe(obj):
    """Best-effort conversion of backtest result data to JSON-serializable values."""
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # Import config first and scope the universes it controls to just this
    # symbol. This subprocess has its own memory space, so it never affects
    # the live bot's config.
    import config
    config.MOMENTUM_UNIVERSE = [args.symbol]
    config.NEWS_SENTIMENT_UNIVERSE = [args.symbol]

    from lumibot.backtesting import YahooDataBacktesting
    from lumibot.entities import Asset
    from strategy_manager import MultiStrategyBot

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    benchmark_asset = Asset(symbol=args.symbol, asset_type="stock")

    try:
        result, strategy = MultiStrategyBot.backtest(
            datasource_class=YahooDataBacktesting,
            backtesting_start=start,
            backtesting_end=end,
            benchmark_asset=benchmark_asset,
            show_plot=False,
            show_tearsheet=False,
            save_tearsheet=False,
        )
        output = {
            "success": True,
            "symbol": args.symbol,
            "start": args.start,
            "end": args.end,
            "result": _make_json_safe(result),
        }
    except Exception as e:
        output = {
            "success": False,
            "symbol": args.symbol,
            "error": str(e),
        }

    with open(args.output, "w") as f:
        json.dump(output, f, default=str, indent=2)


if __name__ == "__main__":
    main()
