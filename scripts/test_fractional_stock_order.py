"""Standalone test: does IB actually accept a cash-quantity (fractional-
equivalent) STOCK order for THIS account? (2026-09-07)

Background: Lumibot's own IB broker integration never sends IB's cashQty
order field for stocks/ETFs — it always sends a whole-share totalQuantity,
which is why every stock priced beyond what the account's buying power
covers in 3+ whole shares got excluded from today's small-account
research. IB itself supports fractional-equivalent trading via cashQty
(confirmed by IB's own documentation, and this project ALREADY has a
working cashQty order path for crypto — see ib_side_channel_trader.py's
_place_entry). ib_connector.py's OrderRequest/place_order now support the
same cash_amount field for STOCK/ETF (see OrderRequest's own docstring).

What's still UNCONFIRMED: whether fractional-share trading is actually
enabled/eligible for this specific account (a non-US IBKR entity, per the
KID/PRIIPs evidence found earlier), and whether the specific test symbol
is on IB's eligible list. This script finds out empirically.

IMPORTANT — single IB login constraint: this project's IB Gateway only
allows ONE login at a time. Do NOT run this script while bot_runner.py's
own tmux session is up, or this connection attempt will fail/interfere
with the live bot's own connection. Confirm with `tmux has-session -t
lumibot` (should say "can't find session") before running for real.

Usage:
    python scripts/test_fractional_stock_order.py --dry-run   # safe, no live order, run anytime
    python scripts/test_fractional_stock_order.py --live      # places a REAL small order — market hours only, bot must be stopped

Defaults to a small (~$10) cash amount on a liquid, cheap-ish test symbol
so a live test risks as little as possible while still being a real,
tradeable-sized order.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("test_fractional_stock_order")

TEST_SYMBOL = "INTC"   # liquid, one of the few affordable-in-whole-shares names found earlier — a good low-risk first probe
TEST_CASH_AMOUNT = 10.0  # small real dollar amount for the live test
# Distinct clientId, never used by any live component of this project
# (main broker=5, pod_connector=6, its forex_app=7) — avoids any
# collision even if run by mistake while the bot is up.
TEST_CLIENT_ID = 9


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Build the order, print it, submit nothing. Safe anytime.")
    mode.add_argument("--live", action="store_true", help="Place a REAL order (small, paper account). Market hours only, bot must be stopped.")
    parser.add_argument("--symbol", default=TEST_SYMBOL)
    parser.add_argument("--cash-amount", type=float, default=TEST_CASH_AMOUNT)
    args = parser.parse_args()

    if args.live:
        import subprocess
        check = subprocess.run(["tmux", "has-session", "-t", "lumibot"], capture_output=True)
        if check.returncode == 0:
            logger.error(
                "The live bot's tmux session ('lumibot') is currently running. "
                "IB only allows one login at a time — refusing to start a second "
                "connection. Stop the bot first (or use --dry-run, which needs no "
                "connection at all)."
            )
            sys.exit(1)

    from ib_connector import IBConnector, Instrument, AssetClass, OrderRequest

    instrument = Instrument(args.symbol, AssetClass.STOCK)
    order_request = OrderRequest(
        instrument=instrument, side="BUY", quantity=0,  # quantity ignored when cash_amount is set
        cash_amount=args.cash_amount,
    )

    if args.dry_run:
        # dry_run needs a connected app object to resolve contract/order-
        # building helpers, but never calls placeOrder — safe with no
        # market open and no risk of a duplicate IB login mattering, since
        # nothing is transmitted. Still connects (a real IB Gateway
        # session), so the single-login caveat above still nominally
        # applies if the live bot happens to be up; not enforced here
        # since nothing this call does can conflict with a live order.
        connector = IBConnector(
            ip=os.environ.get("INTERACTIVE_BROKERS_IP", "127.0.0.1"),
            port=int(os.environ["INTERACTIVE_BROKERS_PORT"]),
            client_id=TEST_CLIENT_ID,
        )
        if not connector.connect():
            logger.error("Could not connect to IB Gateway — is it running? (scripts/start_ib_gateway.sh)")
            sys.exit(1)
        try:
            contract, order = connector.place_order(order_request, dry_run=True)
            logger.info(f"Contract built: symbol={contract.symbol} secType={contract.secType} currency={contract.currency}")
            logger.info(f"Order built: action={order.action} orderType={order.orderType} "
                        f"cashQty={order.cashQty} totalQuantity={order.totalQuantity} tif={order.tif}")
            logger.info("Dry run complete — no order was submitted to IB. Construction looks correct.")
        finally:
            connector.disconnect()
        return

    # --live path
    logger.warning(f"LIVE MODE: about to place a REAL ${args.cash_amount:.2f} cash-quantity BUY order for {args.symbol} on the PAPER account.")
    connector = IBConnector(
        ip=os.environ.get("INTERACTIVE_BROKERS_IP", "127.0.0.1"),
        port=int(os.environ["INTERACTIVE_BROKERS_PORT"]),
        client_id=TEST_CLIENT_ID,
    )
    if not connector.connect():
        logger.error("Could not connect to IB Gateway — is it running? (scripts/start_ib_gateway.sh)")
        sys.exit(1)
    try:
        result = connector.place_order(order_request, timeout=20)
        logger.info(f"RESULT: status={result.status} filled_quantity={result.filled_quantity} "
                    f"avg_fill_price={result.avg_fill_price} raw_error={result.raw_error}")
        if result.status == "Filled":
            logger.info(
                f"SUCCESS: fractional/cash-quantity stock orders WORK on this account. "
                f"Filled {result.filled_quantity} shares of {args.symbol} for ~${args.cash_amount:.2f}."
            )
        elif result.raw_error:
            code, msg = result.raw_error
            logger.warning(
                f"REJECTED (IB error {code}: {msg}) — fractional trading likely isn't "
                f"enabled for this account or this symbol isn't eligible. This confirms "
                f"the whole-share-only constraint is real for this account, not just a "
                f"Lumibot integration gap."
            )
        else:
            logger.warning(f"Did not fill and no specific error captured — status={result.status}")
    finally:
        connector.disconnect()


if __name__ == "__main__":
    main()
