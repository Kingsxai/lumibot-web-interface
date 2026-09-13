"""Shared loader for the per-strategy symbol universes produced by
symbol_screener.py (Phase 3). Each of the six fixed-universe strategies
(scalping, breakout, mean_reversion, vwap, gap_and_go, reversal) calls this
once at import time instead of hardcoding its own 3-symbol list inline.

Falls back to the strategy's original hardcoded list if symbol_universes.json
doesn't exist yet (screener never run) or doesn't have an entry for it, so
nothing breaks before the screener's first run.
"""

import json
import os

_UNIVERSES_PATH = os.path.join(os.path.dirname(__file__), "symbol_universes.json")

# Confirmed live 2026-09-02 (IB error 201, "No Trading Permission... This
# product does not have a KID in English or in a language approved for
# your country"): this IB account cannot buy US-domiciled ETFs lacking a
# PRIIPs-compliant KID, a hard account-level regulatory block, not a
# per-strategy performance judgment (unlike e.g. mean_reversion's own
# separate DIA exclusion). QQQ was the one directly confirmed rejected;
# SPY/DIA/GLD/USO/IWM are the same category of product (US-domiciled
# retail ETF, no EU/UK KID) and near-certain to reject identically —
# every strategy had been generating "confirmed" BUY signals for these
# that could never actually fill, which is exactly what surfaced this
# (user noticed a "confirmed" QQQ buy with no real trade). Filtered here,
# centrally, so it applies regardless of whether a symbol comes from the
# screener's JSON output or a strategy's own hardcoded fallback list.
KID_RESTRICTED_SYMBOLS = {"SPY", "QQQ", "DIA", "GLD", "USO", "IWM"}


def load_universe(strategy_name: str, default: list) -> list:
    if not os.path.exists(_UNIVERSES_PATH):
        universe = default
    else:
        try:
            with open(_UNIVERSES_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            universe = default
        else:
            universe = data.get(strategy_name) or default
    filtered = [s for s in universe if s not in KID_RESTRICTED_SYMBOLS]
    return filtered or [s for s in default if s not in KID_RESTRICTED_SYMBOLS] or default
