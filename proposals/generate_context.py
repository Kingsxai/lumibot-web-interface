"""Group 2's daily context gathering -- plain, deterministic, NO AI
(2026-09-07). Reads recent signals from signals.db (read-only, safe even
if the live bot isn't running right now) so the daily proposal agent has
real, current data to reason about. Writes proposals/context_YYYY-MM-DD.json.

This is the Group 2 equivalent of Group 1's scan_and_prepare.py -- same
split between "gather real data" (no judgment) and "the agent decides"
(a separate daily invocation reading this file).
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "signals.db")


def recent_signals(hours=24):
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT timestamp, strategy_name, symbol, action, confirmed, entry_price "
        "FROM signals WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 100",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    output = {
        "date": date.today().isoformat(),
        "generated_at": datetime.now().isoformat(),
        "recent_signals_24h": recent_signals(24),
        "note": (
            "Read via direct SQLite access to signals.db -- works whether or not "
            "the live bot process is currently running. If recent_signals_24h is "
            "empty, either the bot hasn't run recently or the pods (pod_stock/"
            "pod_etf) haven't fired yet -- not necessarily an error."
        ),
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"context_{output['date']}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {out_path} ({len(output['recent_signals_24h'])} recent signals)")


if __name__ == "__main__":
    main()
