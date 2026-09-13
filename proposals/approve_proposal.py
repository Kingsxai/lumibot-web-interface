"""Human-triggered execution of a Group 2 proposal (2026-09-07).

Reuses the EXISTING, already-production `POST /api/orders` endpoint --
per the plan, Group 2's approved trades go through the same real path
the dashboard's own manual order form already uses, not a new parallel
one. Requires the live bot process (bot_runner.py) to actually be
running, same as using the dashboard directly would.

Usage:
    python proposals/approve_proposal.py --symbol AAPL --side buy --quantity 1

This is deliberately a manual, per-trade CLI action -- read
proposals/YYYY-MM-DD.md yourself first, decide, then run this for the
specific ones you want. There's no "approve the whole file" shortcut on
purpose, since Group 2's whole design (per the plan) is a real per-item
review, unlike Group 1's once-a-day batch glance.
"""
import argparse
import os
import requests
from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", required=True, choices=["buy", "sell"])
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--host", default=os.environ.get("FLASK_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("FLASK_PORT", "5000"))
    args = parser.parse_args()

    api_key = os.environ.get("DASHBOARD_API_KEY")
    if not api_key:
        print("DASHBOARD_API_KEY not set in .env -- required to call the live API.")
        return

    url = f"http://{args.host}:{args.port}/api/orders"
    resp = requests.post(
        url,
        headers={"X-API-Key": api_key},
        json={"symbol": args.symbol, "side": args.side, "quantity": args.quantity},
        timeout=10,
    )
    print(f"Status: {resp.status_code}")
    print(resp.text)


if __name__ == "__main__":
    main()
