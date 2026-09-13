# lumibot-web-interface — read this first

This is a multi-strategy paper-trading bot (Lumibot), **Alpaca-only**
(`BROKER_PROVIDER=alpaca` in `.env`), with a Flask/SocketIO dashboard. A
brief Interactive Brokers migration (2026-09-02 through 2026-09-10, 34
international markets + forex + a 5-category pod system) was fully
reverted back to Alpaca-only — IB's per-share commission structure didn't
fit this account's thin scalping margins on a small account. Every
IB-only file (scanners, `ib_connector.py`, `ib_side_channel_trader.py`,
`ibapi_forex/`) is gone from disk (backed up locally by the user, then
removed from the VM); IB-related code paths still present below are
dormant remnants, imported defensively and gated so nothing crashes, not
active behavior. **Before doing any work in this project, read through the
code below to build a current, accurate understanding — don't rely on
memory notes alone.** Memory captures history, decisions, and *why*
things are the way they are; it can go stale on *what the code actually
does right now* — this file has gone stale that exact way three times
now: 2026-09-07 (still describing a 7-strategy Alpaca-only bot after an
8th strategy shipped), 2026-09-13 morning (still describing the IB
dual-broker architecture three days after it was reverted), and
2026-09-13 later the same day (missing `symbol_research.py` and the repo
reorganization below, both added after the morning's rewrite). Last
verified against the actual code 2026-09-13 (late evening, after the
`closed_trades` cleanup under Live operation below). Use memory for
context and rationale; use the files below for ground truth.

**Repo state**: this repo was previously on branch `Alpaca-only-project`
plus a separate `alpaca-only-v2.0` branch that diverged from `main` for
most of 2026-09-13's work (sandbox reorganization, README/requirements
fixes, `symbol_research.py`). That divergence was merged into `main` the
same day (GitHub PR #1) — `main` is now current and authoritative again,
not behind. `lumibot-alpaca-ai` (Group 1, see #7) also got its own
private GitHub repo for the first time this session
(`github.com/Kingsxai/lumibot-alpaca-ai`) — it never had one before.

## Reading order

1. **`bot_runner.py`** — entry point. Loads `.env`, connects the broker
   (`initialize_broker()` reads `BROKER_PROVIDER`, defaults to `alpaca`;
   an `interactive_brokers` branch still exists but is dormant — nothing
   currently sets that env var), starts the strategy bot in a background
   thread, starts the Flask/SocketIO server. This file also still carries
   several runtime monkey-patches on Lumibot's own IB broker class (a
   delayed-tick-type remap + non-positive-price guard on
   `IBWrapper.tickPrice`, a short-TTL cache/lock around
   `_get_balances_at_broker`) and still polls for `self.ib_aux` to wire
   into the dashboard API (`init_extended_hours_trader`/
   `init_international_markets_trader`) and consumes a watchdog-only
   safety-resume flag — all of this only does anything if
   `BROKER_PROVIDER=interactive_brokers` is ever set again (`self.ib_aux`
   is `None` on Alpaca, so the wiring is a no-op). Read the inline
   comments if IB is ever revisited; the real incidents they were written
   for are still explained there.
2. **`config.py`** — all tunables: position sizing (`MAX_POSITION_SIZE`
   = 37% of portfolio per position, dashboard-editable, raised from 10%
   on 2026-09-12), a flat `HARD_POSITION_CEILING_GBP` = $250 hard dollar
   ceiling on any single position (name kept from the IB/GBP era, now
   just a USD ceiling reused as-is post-reversal — this is the number
   that actually binds at this account's size, not the percentage),
   `CAPITAL_SANITY_THRESHOLD_GBP`, stop-loss/take-profit defaults,
   per-strategy allocation percentages, `STRATEGY_PRIORITY_RANK` (see
   #3's priority-takeover note), and the asset-class-rider toggles
   (`ENABLE_VIX_RIDER` etc., see #8). `INTERNATIONAL_MARKETS`,
   `FOREX_PAIRS`, and the IB-connection constants are IB-era leftovers,
   unused since the reversal — don't treat their presence as evidence
   international/forex trading is active.
3. **`strategy_manager.py`** — the core, ~3100 lines. `MultiStrategyBot` (a
   Lumibot `Strategy` subclass) orchestrates every sub-strategy each
   iteration cycle (`on_trading_iteration`), handles signal logging,
   secondary-indicator confirmation, the Phase 4 meta-model confidence
   gate, the 2026-09-01 per-symbol regime-precedence controller, the
   2026-09-12 fixed-priority takeover system (`_check_priority_takeovers`
   — a higher-`STRATEGY_PRIORITY_RANK` strategy can take over an open
   position's exits when multiple strategies signal the same symbol the
   same cycle; never liquidates/re-enters, unit-tested but the priority
   order itself is unvalidated against real trading), bracket order
   placement/tracking, the same-cycle capital-reservation/symbol-lock,
   and the cross-strategy exclusive ownership lock. The five `pod_*`
   toggles and `self.ib_aux`/`self.pod_connector` (both `None` on Alpaca)
   are permanently inert now — `POD_MODULES` builds to `{}` since the pod
   scanner modules don't exist on disk, so `_run_pod_strategies` always
   no-ops regardless of toggle state. Read this fully — most bugs and
   design decisions live here.
4. **The strategy files** — 9 core strategies, each with an `.analyze()`
   method returning `{symbol: "BUY"/"SELL"/"HOLD"}`, all live-tested:
   - `momentum_allocator.py`, `breakout.py`, `mean_reversion.py`,
     `vwap.py`, `gap_and_go.py`, `reversal.py`, `market_profile.py`,
     `scalping.py` — all 8 pull their candidate symbols from
     `regime_router.py`'s shared, regime-routed pool (see #5) rather than
     each keeping its own universe.
   - `scalping_v2.py` (added 2026-09-08, own dashboard toggle seeded OFF)
     — MACD histogram flip + Stoch RSI K/D cross confluence on 1-minute
     bars; uses its own fixed-fallback universe via `symbol_universe.py`'s
     `load_universe`, not `regime_router.py`.
   - **`sentiment` is NOT a strategy** (changed 2026-09-01) — `news_sentiment.py`'s
     `NewsSentimentAnalyzer` is purely a symbol-selection input:
     `scalping.py`'s `_sentiment_candidates()` calls into it to add
     symbols with a current positive sentiment reading as extra scan
     candidates each cycle. It never places its own orders, has no
     toggle, no regime assignment, and doesn't appear in
     `signal_logger.ALL_STRATEGY_NAMES`. `NewsSentimentAnalyzer.analyze()`
     itself still exists and is still callable (harmlessly, read-only)
     via `/api/strategy-preview`.
   - **`sentiment.py` is dead code** — confirmed not imported anywhere, an
     earlier cruder unused prototype of `news_sentiment.py`'s scoring
     logic. Don't confuse the two.
   - **14 more strategies in `extended_strategies_live.py`** (2026-09-11,
     from a sizing/collision-simulation marathon — `lab_range`,
     `supply_demand`, `supertrend_200ema`, `fakeout_breakout_fib`,
     `macd_200ema`, `ema_ribbon_smi`, `discount_zone`,
     `breakout_chandelier`, `volume_divergence_grab`, `asymmetric_dual`,
     `rigorous_rr`, `volume_absorption`, `candle_taxonomy`,
     `volume_profile`) — all minute-bar, all pull from `regime_router.py`,
     all seeded OFF (real code, never live-exercised; check current
     toggle state before assuming any of them is actually trading).
   - The five `pod_*` names in `ALL_STRATEGY_NAMES` are IB-pod leftovers
     (see #3) — permanently inert, not real strategies to reason about.
5. **Supporting modules**: `regime_router.py` (2026-09-09, the shared
   candidate pool for the 8 original core strategies plus the 14
   extended ones — live-discovers candidates via Alpaca's screener,
   classifies each one's current regime via `regime_matcher.py`, routes
   it to whichever strategy(ies) that regime is assigned to, and drops
   anything flagged by its efficiency-ratio "chaos filter" for extreme
   whipsaw price action; disk-cached so a restart doesn't force a cold
   re-scan), `regime_indicators.py` (shared technical indicators; also
   holds `MINIMAL_TAKE_PROFIT_PCT`, the fixed take-profit that
   `mean_reversion.py`/`vwap.py`/`reversal.py` use instead of their own
   validated SMA20/2R targets — set to 0.02% on 2026-09-09 as a user
   experiment, raised to 0.5% on 2026-09-13 by user decision; the only
   Alpaca-path evidence for 0.02% is reversal's 3 take_profit exits on
   2026-09-11 at exactly $0.00 each, n=3 — the 44 one-minute
   mean_reversion "take_profit" exits on 09-04/09-08 were IB-era
   side-channel international trades, NOT this setting; the stops were
   never touched, so reward:risk on those three is still far below 1:1,
   and under the original targets backtests hold for days — reversal
   averaged 2.2 days to its own exit signal),
   `regime_matcher.py` (per-symbol regime classification +
   `STRATEGY_REGIMES`), `confirmation.py` (secondary-indicator
   confirmation gate — also applies the VIX rider uniformly to every
   core/extended strategy), `asset_class_riders.py` (VIX rider is the
   only one with a live caller; gold-macro/crypto-rotation riders are
   enabled in `config.py` but have no live caller since the pod system
   they were built for is gone; forex-carry is a stub, always off),
   `meta_model.py` + `train_meta_model.py` (Phase 4 confidence models),
   `signal_logger.py` (`signals.db`, `ALL_STRATEGY_NAMES`, the pod
   toggles), `symbol_screener.py` + `symbol_universe.py` (Alpaca-based
   screener candidates and scalping_v2's fixed-universe fallback).
   `portfolio_manager.py` and `risk_rules.py` were built 2026-09-03 for
   the IB pod system's cross-category capital allocation and per-category
   stop/take-profit — both say so in their own module docstrings ("not
   wired into the live trading path"), and with the pod system now
   permanently inert, both are confirmed orphaned: still imported by
   `strategy_manager.py`, never actually called in the live path.
   `portfolio_manager.py` also has a stale hardcoded 10%-of-portfolio
   constant that no longer matches `config.MAX_POSITION_SIZE` (now 37%)
   — harmless since nothing calls it, but don't use it as a reference for
   the real live cap. `order_executor.py` no longer exists (was IB-only).
   `symbol_research.py` (2026-09-13, explicit user request) — an LLM-based
   ICT/Smart-Money-Concepts trap-vs-genuine-move research rider, targeted
   specifically at the 11 of 14 extended strategies with
   `use_regime_chaos_filter=False` (the chaos filter was measured to help
   only 4/16 strategies and hurt 12 — this fills that gap rather than
   layering on strategies it already helps). Runs as its own standalone
   cron job (`scripts/run_symbol_research.sh`, every 5 min, event-driven
   on the 15-symbol rotating universe actually changing) — never inside
   the live 60s loop; `strategy_manager.py` only ever does a cheap
   `symbol_research.get_research()` cache read. Each real research call
   feeds one `claude -p` invocation (no tool access, real data embedded
   directly in the prompt) three real inputs: recent daily bars, SEC
   Form 4 insider transactions (free, discretionary-vs-scheduled-10b5-1
   aware), and options open interest by strike (Alpaca's Trading API,
   confirmed free on this account's plan — real trade volume/OPRA data
   is NOT, needs a paid Algo Trader Plus subscription, not used here).
   Consumed on both sides of a trade: gates + damps the take-profit at
   entry (`config.SYMBOL_RESEARCH_TP_MAX_MULTIPLIER`, currently 1.5), and
   a late-arriving verdict also acts on an already-open position — a
   trap verdict closes it immediately regardless of current P&L, a
   genuine-move verdict widens its take-profit in place
   (`_check_symbol_research_exits_and_tp_updates`). Fails open everywhere,
   same convention as every other rider here.
6. **`api.py`** — REST/WebSocket API for the dashboard
   (`templates/dashboard.html`), ~1300 lines, 30+ routes (positions,
   orders, strategy toggles/risk, confidence/regime stats, pause/resume,
   live-vs-paper account-mode + credentials storage, test-all-strategies
   sweep). The extended-hours/international toggle routes still exist but
   have nothing to drive since `self.ib_aux` is `None` on Alpaca. Requires
   `X-API-Key` (or `?key=`) matching `DASHBOARD_API_KEY` in `.env` on
   every `/api/*` route and the socket.io handshake.
7. **The Phase 5 AI integration** ("Phase 5" is this project's own term,
   unrelated to the older roadmap's real-money Phase 5/6 — see project
   memory) — a live A/B experiment comparing full AI trading autonomy
   against a human-gated process, both paper-money-only, unaffected by
   the broker reversal above (both groups were already Alpaca/IB as
   designed):
   - **Group 1** (`/home/VMbot01/lumibot-alpaca-ai/`, a separate, isolated
     repo, NOT under this directory) — Alpaca paper account, a once-daily
     Claude Code agent (cron) that can propose AND (via a separate
     human-run `execute_approved_trades.py --approve` step) execute
     trades within hard-coded guardrails (`risk_ceilings.py`). A separate
     deterministic `manage_positions.py` cron enforces intraday stops.
   - **Group 2** (`proposals/` inside *this* repo) — `generate_context.py`
     reads real `signals.db` data; a daily cron-invoked Claude Code agent
     (`daily_proposal_prompt.md`, no Bash access) writes
     `proposals/YYYY-MM-DD.md`; `approve_proposal.py` (CLI, human-run)
     submits an approved proposal through the dashboard's own
     `POST /api/orders` endpoint.
   - Both groups are capped to a $136 virtual/real capital ceiling for
     comparability. Full design history is in project memory
     (`project_phase5_autonomous_ai_trading` and linked memories) — read
     that before changing anything here.
8. **Operational scripts** (`scripts/`): `start_bot_cron.sh` (launches the
   bot 5 min before market open, cron, checks real ET time since this
   cron has no per-crontab timezone support), `watchdog.sh` (self-healing
   restart on a stale iteration heartbeat, cron every minute, market-
   hours-aware; its IB-Gateway health check is gated on `BROKER_PROVIDER`
   from `.env` and currently dormant under Alpaca), `test_fractional_stock_order.py`
   (one-off live fractional/cash-quantity order probe).
9. **`backtest_runner.py`** / `label_signals.py` / `run_multi_backtest.py`
   — backtesting and retrospective outcome labeling, for the daily-bar
   strategies only (see Live operation below). All the one-off
   exploratory scripts from the strategy-testing marathons (formerly
   loose `sandbox_*.py`/`marathon_*.py`/`run_*.py`/`gather_*.py` files at
   the repo root, plus `hold_time_analysis/`) now live under `sandbox/`
   (2026-09-13 reorganization), grouped into subfolders by test topic —
   not part of the live system, not worth enumerating here, and
   deliberately excluded from git (never pushed, local-only on this VM).

## Live operation, in one sentence each

- Starting `bot_runner.py` only starts the process — trading itself
  requires a dashboard "Start" click (`POST /api/start`), a deliberate
  manual gate.
- `self.set_market("24/5")` — the iteration loop runs continuously
  Monday-Friday, not just NASDAQ regular hours; each strategy/subsystem
  gates its own session-appropriate behavior internally rather than the
  loop itself sleeping outside 09:30-16:00 ET.
- `sleeptime` must be a string with an explicit unit (e.g. `"60S"`) —
  Lumibot interprets a bare int as **minutes**, not seconds (this exact
  mistake caused a real multi-hour "silent hang" investigation before the
  actual cause was found).
- 6 of the 9 core strategies decide off **daily bars**, not intraday
  ticks, even live — decisions are stable within any given day. The
  exceptions are `scalping.py`, `market_profile.py`, and `scalping_v2.py`
  (added 2026-09-08) — all three run off minute bars and can change their
  decision within the same day. All 14 strategies in
  `extended_strategies_live.py` are minute-bar too.
- Every minute-bar strategy (`scalping.py`, `market_profile.py`,
  `scalping_v2.py`, and all 14 in `extended_strategies_live.py`) skips
  itself entirely during any Lumibot backtest (`is_backtesting` check) —
  never remove this. `YahooDataBacktesting`'s underlying cache is keyed by
  asset only, not `(asset, timestep)`, so once another strategy caches a
  symbol's daily bars, a later "minute" request for that same symbol
  silently returns the cached daily data instead of failing — fabricating
  believable-looking signals off daily volatility. Minute-bar strategies
  can only be validated against real minute data directly (Alpaca's own
  historical API), never through `backtest_runner.py`/`run_multi_backtest.py`.
- `confidence-stats`'s "fired" count is NOT proof an order executed — it
  increments before order placement is even attempted, with no rollback
  on failure. Verify real execution against the broker's own order/
  position data directly when it matters.
- `closed_trades` in `signals.db` is live-only as of 2026-09-13 —
  `on_filled_order` now returns before `log_closed_trade` when
  `is_backtesting`. Before that, backtest runs wrote into the same table
  (310 of 437 rows were backtest — 2024 `closed_at` dates, or written in
  the 2026-09-09 12:xx burst with 2024/NULL `opened_at`), which inflated
  every "live" per-strategy stat the dashboard showed. Those rows were
  purged (127 real rows remained; pre-purge backup at
  `~/signals_db_backup_2026-09-13_1816.zip`). The `signals` table still
  accumulates backtest rows on purpose (`label_signals.py`/meta-model
  read them) — only `closed_trades` is guarded. Any live-vs-backtest
  comparison made before this date was built on mixed data.
- Positions are broker-level (per symbol, not per strategy) — an
  exclusive ownership lock in `_close_position` prevents one strategy
  from closing another's position; only the opening strategy's own
  signal, the bracket order's own stop-loss/take-profit, the news
  emergency stop (below), or a higher-priority strategy's takeover
  (`_check_priority_takeovers`, #3) can affect it.
- `_check_news_emergency_stop()` liquidates ANY open position, any
  strategy, the moment sentiment on that symbol falls to
  `config.NEWS_EMERGENCY_STOP_THRESHOLD` (-0.7) — bypasses the ownership
  lock on purpose, same as the bracket order's own stop-loss/take-profit.
  Not the same thing as scalping's sentiment rider or mean_reversion's
  momentum filter — those gate NEW entries; this reacts to positions
  already open.
- Lumibot's own broker/data-source layer can call `get_last_price`/
  position-valuation directly on `self.broker`, bypassing this project's
  `Strategy`-subclass overrides entirely — confirmed live during the IB
  era (LOV, then EURUSD/USDCZK noise on symbols that no longer trade
  under Alpaca-only) but the underlying gap in Lumibot itself isn't
  broker-specific; watch for the same symptom (silent stale-price/"Unable
  to get data" noise) recurring on an Alpaca symbol before assuming it
  can't happen. Not fixable from the `Strategy` subclass; needs a broker/
  data-source-level monkey-patch when it's worth fixing (see
  `bot_runner.py`'s existing IB-era patches for the established
  technique).
- `symbol_research.py`'s cron job (every 5 min) is the only thing in this
  project that calls out to an LLM from the live system — everything
  else is deterministic. It never runs inside `on_trading_iteration`
  itself; check `symbol_research.log` / `symbol_research_cache.json` age
  if a research verdict seems stale, not the bot's own logs.
- This is a **paper-trading account** (Alpaca), but connected to real
  brokerage infrastructure — live bot start/stop, `.env`/credentials, and
  any destructive git operation require the user's explicit yes each
  time, even if a previous session was told the same thing.
- **The bot is long-only by design, and the paper account hides real
  small-account rules** (confirmed with the user 2026-09-13): shorting
  needs a margin account, which needs $2,000 minimum equity — at $136
  this is a cash account, no shorts, ever (`strategy_manager.py` already
  flattens any accidental short). Under $25k in a margin account the
  Pattern Day Trader rule caps day trades at 3 per rolling 5 business
  days; in a cash account there is no PDT but sale proceeds settle T+1,
  so a real $136 account gets roughly ONE round-trip per day across all
  strategies combined. Alpaca paper enforces none of this — it simulates
  margin with unlimited day trades — so every minute-bar strategy's live
  paper record (e.g. mean_reversion's 51 trades at ~8-min holds) is not
  reproducible with real money at this size. Before any real-money phase
  the strategy mix must be re-simulated under that frequency cap. Never
  propose a short strategy; long inverse ETFs (SQQQ/SPXS/SOXS/TZA) are
  the only cash-account route to downside exposure.
