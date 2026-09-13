"""Per-category take-profit/stop-loss rules (2026-09-03) — the piece
between portfolio_manager.py's dollar allocations and real order
placement (order_executor.py). New, isolated module, not wired into
the live trading path.

Why per-category, not one global rule
--------------------------------------
signal_logger.py already has a working pattern for this at the STRATEGY
level (get_strategy_risk/set_strategy_risk_override: a global default —
config.STOP_LOSS_PERCENT=5%, config.TAKE_PROFIT_PERCENT=7.5% — with a
per-strategy override table). This module adapts that same shape
(default + override, easy to extend to a persisted store later) but
keyed by the five pods' CATEGORIES instead of the 8 strategy names — a
category-level knob didn't exist before today because there was only
one asset class (US stocks) live-trading.

The user's own words: stop/take-profit "each might have different"
rules. That's not arbitrary — the five pods found genuinely different
real volatility during today's builds (see each pod's own
MAX_ANNUALIZED_VOLATILITY comment for the observed range, not just the
noise-filter ceiling):
    Forex:      5.5%-8.0% annualized   (forex_scanner.py)
    ETF:        12.1%-18.2% annualized (etf_scanner.py)
    Commodity:  20.1%-30.8% annualized (commodity_scanner.py)
    Crypto:     39.5%-74.5% annualized (crypto_scanner.py)
A stop sized for calm Forex movement would be so tight on Crypto it'd
get stopped out by routine noise; a stop wide enough to survive real
Crypto swings would be recklessly loose on Forex. One global percentage
can't serve both.

The math
--------
stop_loss_pct = (annualized_vol / sqrt(252)) * STOP_MULTIPLIER
take_profit_pct = stop_loss_pct * RISK_REWARD_RATIO

annualized_vol / sqrt(252) converts each category's annualized
volatility to an implied ONE-DAY move — the standard conversion (daily
variance accumulates additively under a random-walk assumption, so
annualized = daily * sqrt(252) trading days; the same sqrt(252) already
used by every pod's own volatility calc, see e.g. etf_scanner.py's
_liquidity_and_volatility). STOP_MULTIPLIER=3.0 is a documented,
reviewable choice — "three days' worth of typical daily movement" is a
plain, defensible margin against normal noise without being arbitrary;
not derived from backtested data (none exists yet for these pods,
they're brand new today), flag this as a starting point the user should
revisit once real trades accumulate.

RISK_REWARD_RATIO=1.5 matches the existing global config exactly
(config.TAKE_PROFIT_PERCENT / config.STOP_LOSS_PERCENT = 7.5 / 5 = 1.5)
— reused so this module's reward assumption is consistent with what's
already live for the 8 strategies, not a second, different ratio.

STOCK uses config.py's existing STOP_LOSS_PERCENT/TAKE_PROFIT_PERCENT
directly (5%/7.5%), not the formula above — Stock is the category the
original numbers were calibrated for; recomputing them from a volatility
midpoint would just reproduce roughly the same numbers with extra steps
and a chance of drifting from the already-proven live values.
"""
import math
from dataclasses import dataclass
from typing import Dict, Optional

import config

CATEGORIES = ("STOCK", "ETF", "COMMODITY", "FOREX", "CRYPTO")

STOP_MULTIPLIER = 3.0
RISK_REWARD_RATIO = config.TAKE_PROFIT_PERCENT / config.STOP_LOSS_PERCENT  # 1.5, reused not re-derived

# Midpoint of each pod's real observed annualized-volatility range (see
# module docstring for the exact figures and which pod's file they came
# from) — used only to DERIVE the defaults below once; the derivation
# isn't re-run at import time so the numbers stay fixed and auditable
# rather than silently shifting if a pod's own constants ever change.
_OBSERVED_VOL_MIDPOINT = {
    "FOREX": 0.0675,      # (0.055 + 0.080) / 2
    "ETF": 0.150,          # (0.121 + 0.182) / 2, rounded
    "COMMODITY": 0.2545,   # (0.201 + 0.308) / 2
    "CRYPTO": 0.570,       # (0.395 + 0.745) / 2
}


def _derive(annualized_vol_midpoint: float) -> "RiskRule":
    daily_vol = annualized_vol_midpoint / math.sqrt(252)
    stop_loss_pct = round(daily_vol * STOP_MULTIPLIER, 4)
    take_profit_pct = round(stop_loss_pct * RISK_REWARD_RATIO, 4)
    return RiskRule(stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, is_custom=False)


@dataclass
class RiskRule:
    stop_loss_pct: float
    take_profit_pct: float
    is_custom: bool = False


def _default_rules() -> Dict[str, "RiskRule"]:
    return {
        "STOCK": RiskRule(
            stop_loss_pct=config.STOP_LOSS_PERCENT, take_profit_pct=config.TAKE_PROFIT_PERCENT,
        ),
        "ETF": _derive(_OBSERVED_VOL_MIDPOINT["ETF"]),
        "COMMODITY": _derive(_OBSERVED_VOL_MIDPOINT["COMMODITY"]),
        "FOREX": _derive(_OBSERVED_VOL_MIDPOINT["FOREX"]),
        "CRYPTO": _derive(_OBSERVED_VOL_MIDPOINT["CRYPTO"]),
    }


# Module-level default table, computed once at import — mirrors
# signal_logger.py's get_strategy_risk shape (default + override) but
# in-memory only for now (no persisted override store yet, unlike the
# live per-strategy table) since this whole pipeline isn't wired to a
# running process to override anything for yet. set_category_risk_
# override()/clear_category_risk_override() below exist so a future
# caller (or a persisted-store version of this module) has the same
# shape to grow into without an interface change.
_DEFAULTS = _default_rules()
_OVERRIDES: Dict[str, "RiskRule"] = {}


def get_category_risk(category: str) -> RiskRule:
    """Effective stop-loss/take-profit for a category: its override if
    one is set, otherwise the derived/global default. Same default-plus-
    override shape as signal_logger.get_strategy_risk, deliberately —
    see module docstring."""
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r} — must be one of {CATEGORIES}")
    return _OVERRIDES.get(category, _DEFAULTS[category])


def set_category_risk_override(category: str, stop_loss_pct: float, take_profit_pct: float):
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r} — must be one of {CATEGORIES}")
    _OVERRIDES[category] = RiskRule(stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, is_custom=True)


def clear_category_risk_override(category: str):
    _OVERRIDES.pop(category, None)


def get_all_category_risk() -> Dict[str, RiskRule]:
    return {c: get_category_risk(c) for c in CATEGORIES}


def compute_exit_prices(entry_price: float, side: str, category: str,
                         rule: Optional[RiskRule] = None) -> "tuple[float, float]":
    """(stop_loss_price, take_profit_price) for a long or short entry at
    entry_price. side: "BUY" (long — stop below, target above) or "SELL"
    (short — stop above, target below)."""
    rule = rule or get_category_risk(category)
    if side.upper() == "BUY":
        stop_loss_price = entry_price * (1 - rule.stop_loss_pct)
        take_profit_price = entry_price * (1 + rule.take_profit_pct)
    elif side.upper() == "SELL":
        stop_loss_price = entry_price * (1 + rule.stop_loss_pct)
        take_profit_price = entry_price * (1 - rule.take_profit_pct)
    else:
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    return round(stop_loss_price, 6), round(take_profit_price, 6)
