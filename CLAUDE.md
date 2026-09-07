# lumibot-web-interface — read this first

This is a multi-strategy paper-trading bot (Lumibot, with a **dual broker**
backend — Alpaca or Interactive Brokers, selected by `BROKER_PROVIDER` in
`.env`; currently set to `interactive_brokers`), with a Flask/SocketIO
dashboard. **Before doing any work in this project, read through the code
below to build a current, accurate understanding — don't rely on memory
notes alone.** Memory captures history, decisions, and *why* things are the
way they are; it can go stale on *what the code actually does right now* (a
real example: a memory said the sentiment strategy "needs NEWSAPI_KEY, out
of scope" — the code had already migrated away from that, and the memory
was simply wrong by the time it mattered; this file itself has gone stale
the same way before — last verified against the actual code 2026-09-07,
when it still described a 7-strategy Alpaca-only bot). Use memory for
context and rationale; use the files below for ground truth.

## Reading order

1. **`bot_runner.py`** — entry point. Loads `.env`, connects the broker
   (`initialize_broker()` branches on `BROKER_PROVIDER`: `alpaca` (default)
   or `interactive_brokers`), starts the strategy bot in a background
   thread, starts the Flask/SocketIO server. When IB is selected, this file
   also carries several runtime monkey-patches on Lumibot's own IB broker
   class: a delayed-tick-type remap + non-positive-price guard
   (`IBWrapper.tickPrice`, for accounts without a real-time data
   subscription on every symbol) and a short-TTL cache/lock around
   `_get_balances_at_broker` (Flask's concurrent dashboard polling was
   firing overlapping `reqAccountSummary` calls IB Gateway rejects past
   one at a time). Also wires up `self.ib_aux` (see #7) into the dashboard
   API and consumes a watchdog-only safety-resume flag — read the inline
   comments, they explain real incidents each fix was for.
2. **`config.py`** — all tunables: position sizing, stop-loss/take-profit
   defaults, per-strategy allocation percentages, symbol universes for
   momentum/sentiment, **plus** (2026-09-02 onward) `INTERNATIONAL_MARKETS`
   (34 markets' exchange codes/sessions/universes, IB-only), `FOREX_PAIRS`,
   and the asset-class-rider toggles (`ENABLE_VIX_RIDER` etc., see #8).
3. **`strategy_manager.py`** — the core, ~2300 lines. `MultiStrategyBot` (a
   Lumibot `Strategy` subclass) orchestrates all **8** sub-strategies every
   iteration cycle (`on_trading_iteration`), handles signal logging,
   secondary-indicator confirmation, the Phase 4 meta-model confidence
   gate, the 2026-09-01 per-symbol regime-precedence controller, bracket
   order placement/tracking, the same-cycle capital-reservation/symbol-
   lock, and the cross-strategy exclusive ownership lock. The same method
   also drives, every cycle: `self.ib_aux`'s international/forex/extended-
   hours cycle (#7) and `_run_pod_strategies()`, the 5-category pod scanner
   (#8) — both IB-only, both no-ops on a non-IB broker. Read this fully —
   most bugs and design decisions live here.
4. **The 8 strategy files** — each has a `.analyze()` method returning
   `{symbol: "BUY"/"SELL"/"HOLD"}`:
   - `momentum_allocator.py` (own symbol universe, individually orchestrated)
   - `scalping.py`, `breakout.py`, `mean_reversion.py`, `vwap.py`, `gap_and_go.py`, `reversal.py` (share `symbol_universe.py`'s screener-driven universes, uniformly orchestrated via `_run_strategy`)
   - `market_profile.py` (8th strategy, added later — Auction Market Theory/Value Area breakout; also minute-bar-driven, see Live operation below)
   - **`sentiment` is NOT a strategy** (changed 2026-09-01) — `news_sentiment.py`'s `NewsSentimentAnalyzer` is now purely a symbol-selection input: `scalping.py`'s `_sentiment_candidates()` calls into it to add symbols with a current positive sentiment reading as extra scan candidates each cycle (never merged into `symbol_universes.json` permanently). It never places its own orders, has no toggle, no regime assignment, and doesn't appear in `signal_logger.ALL_STRATEGY_NAMES`. `NewsSentimentAnalyzer.analyze()` itself still exists and is still callable (harmlessly, read-only) via `/api/strategy-preview`.
   - **`sentiment.py` is dead code** — an earlier, cruder unused prototype of `news_sentiment.py`'s scoring logic (confirmed not imported anywhere). Don't confuse the two.
5. **Supporting modules**: `regime_indicators.py` (shared technical indicators for mean_reversion/vwap/reversal), `regime_matcher.py` (per-symbol regime classification + `STRATEGY_REGIMES` used by the precedence controller), `confirmation.py` (secondary-indicator confirmation gate), `asset_class_riders.py` (see #8), `meta_model.py` + `train_meta_model.py` (Phase 4 confidence models, `models/*.pkl`), `signal_logger.py` (`signals.db` — the append-only signal/outcome log, plus settings tables incl. `ALL_STRATEGY_NAMES` and the 5 pod toggles), `symbol_screener.py` + `symbol_universe.py` (dynamic per-strategy symbol universes, `symbol_universes.json`), `portfolio_manager.py` / `risk_rules.py` / `order_executor.py` (newer supporting modules for capital/risk sizing and order placement — not yet given the same close read as the files above, treat their exact current behavior as unverified until you've read them).
6. **The IB international/forex/extended-hours layer** — `ib_side_channel_trader.py` (~1800 lines, `IBAuxTrader` = `self.ib_aux`) is a **second, separate IB Gateway connection** (its own `clientId`) that owns: 34 international equity markets (UK/Australia/Japan/Hong Kong/... — see `config.INTERNATIONAL_MARKETS`), forex (`config.FOREX_PAIRS`), and US extended-hours (pre/post-market) order routing. It also reconciles real broker positions into `self.bracket_orders` on connect/reconnect. `ib_connector.py` (~1300 lines) is IB API plumbing shared with the pod system (#8) — a *different*, independent reqId-keyed client than `ib_side_channel_trader.py`'s own, built separately, not a shared abstraction. `ibapi_forex/` is a **vendored newer copy of IBKR's own official `ibapi` client** (downloaded directly from IBKR, not PyPI) used on a third connection (`clientId=3`) purely because PyPI's pinned `ibapi` (protocol ≤157, Lumibot's own dependency — never touched) can't fetch EUR/USD, GBP/USD, and several other pairs that need protocol 163+.
7. **The 5-category pod system** (2026-09-04 cutover) — `stock_scanner.py` / `etf_scanner.py` / `commodity_scanner.py` / `forex_scanner.py` / `crypto_scanner.py` scan their own instrument universes (via `ib_connector.py`) on a round-robin, self-throttled cadence (`_run_pod_strategies` in `strategy_manager.py`) as a candidate-generation source **independent of** the 8 named strategies above. `STOCK` candidates route through the normal `_place_bracket_order` path; `ETF`/`COMMODITY`/`FOREX`/`CRYPTO` route through `self.ib_aux._place_entry` directly with a real per-instrument IB contract. All five are gated behind their own dashboard toggle (`pod_stock`/`pod_etf`/`pod_commodity`/`pod_forex`/`pod_crypto` in `signal_logger.ALL_STRATEGY_NAMES`), **seeded OFF by default** — this is newer, less live-tested code than the 8 core strategies; check current toggle state before assuming any pod is actually trading. `asset_class_riders.py` provides optional cross-market "second opinion" confirmation for pod entries (VIX for all 8 core strategies too, gold-macro for commodities, crypto-rotation for crypto) — fail-open by design (never blocks on a data problem), a stub-only forex-carry rider exists but is off.
8. **`api.py`** — REST/WebSocket API for the dashboard (`templates/dashboard.html`), ~1250 lines, 30+ routes (positions, orders, strategy toggles/risk, confidence/regime stats, pause/resume, extended-hours/international toggles, live-vs-paper account-mode + credentials storage, test-all-strategies sweep). Requires `X-API-Key` (or `?key=`) matching `DASHBOARD_API_KEY` in `.env` on every `/api/*` route and the socket.io handshake.
9. **The Phase 5 AI integration** ("Phase 5" is this project's own term, unrelated to the older roadmap's real-money Phase 5/6 — see project memory) — a live A/B experiment comparing full AI trading autonomy against a human-gated process, both paper-money-only:
   - **Group 1** (`/home/VMbot01/lumibot-alpaca-ai/`, a separate, isolated repo, NOT under this directory) — Alpaca paper account, a once-daily Claude Code agent (cron) that can propose AND (via a separate human-run `execute_approved_trades.py --approve` step) execute trades within hard-coded guardrails (`risk_ceilings.py`: per-order/daily caps, long-only, mandatory stop-loss, max-loss circuit breaker, self-protecting against the agent's own edits). A separate deterministic (no-AI) `manage_positions.py` cron enforces intraday stops between the once-daily decision cycles.
   - **Group 2** (`proposals/` inside *this* repo) — `generate_context.py` reads real `signals.db` data; a daily cron-invoked Claude Code agent (`daily_proposal_prompt.md`, tool-scoped via `.claude_settings.json` to **no Bash access at all** — structurally cannot place an order) writes `proposals/YYYY-MM-DD.md`; `approve_proposal.py` (CLI, human-run, one trade at a time) submits an approved proposal through the dashboard's own existing `POST /api/orders` endpoint.
   - Both groups are capped to a $136 virtual/real capital ceiling for comparability. Full design history, including a Claude Code safety-classifier block that forced the "propose, don't auto-execute" redesign, is in project memory (`project_phase5_autonomous_ai_trading` and linked memories) — read that before changing anything here.
10. **Operational scripts** (`scripts/`): `start_bot_cron.sh` (launches the bot 5 min before market open, cron, checks real ET time since this cron has no per-crontab timezone support), `watchdog.sh` (self-healing restart on a stale iteration heartbeat, cron every minute, now market-hours- and IB-Gateway-aware), `test_fractional_stock_order.py` (one-off live IB fractional/cash-quantity order probe).
11. **`backtest_runner.py`** / `label_signals.py` / `run_multi_backtest.py` — backtesting and retrospective outcome labeling, for the 6 daily-bar strategies only (see Live operation below). `hold_time_analysis/` holds one-off analysis scripts + their JSON/txt output, not part of the live system. `sandbox_*.py` files, where they still exist, are similarly one-off exploratory scripts.

## Live operation, in one sentence each

- Starting `bot_runner.py` only starts the process — trading itself requires a dashboard "Start" click (`POST /api/start`), a deliberate manual gate.
- Broker selection is `.env`-driven (`BROKER_PROVIDER`), not a code branch you need to maintain per-feature — but IB-only features (`self.ib_aux`, the pod system, `asset_class_riders.py`'s market-wide riders) are literally `None`/no-ops when the broker isn't IB, so don't assume they run under Alpaca.
- `self.set_market("24/5")` (2026-09-03) — the iteration loop now runs continuously Monday-Friday, not just NASDAQ regular hours; each strategy/subsystem gates its own session-appropriate behavior internally (e.g. `_place_bracket_order` routes through IB's outside-regular-hours LMT path when appropriate) rather than the loop itself sleeping outside 09:30-16:00 ET.
- `sleeptime` must be a string with an explicit unit (e.g. `"60S"`) — Lumibot interprets a bare int as **minutes**, not seconds (this exact mistake caused a real multi-hour "silent hang" investigation before the actual cause was found).
- 6 of the 8 core strategies decide off **daily bars**, not intraday ticks, even live — decisions are stable within any given day. The two exceptions are `scalping.py` (`get_historical_prices(symbol, 2, "minute")`) and `market_profile.py` (`get_historical_prices(symbol, PROFILE_LOOKBACK_MINUTES, "minute")`) — both can change their decision within the same day. The 5 pod categories and the IB international/forex layer are separate mechanisms again (see #6/#7 above), not covered by this daily-bar/minute-bar split.
- `scalping.py` (and, by the same reasoning, `market_profile.py`) skip themselves entirely during any Lumibot backtest (`is_backtesting` check) — never remove this. `YahooDataBacktesting`'s underlying cache is keyed by asset only, not `(asset, timestep)`, so once another strategy caches a symbol's daily bars, a later "minute" request for that same symbol silently returns the cached daily data instead of failing — fabricating believable-looking signals off daily volatility (confirmed 2026-09-01: every fake signal landed at exactly 09:30:00). Minute-bar strategies can only be validated against real minute data directly (Alpaca's/IB's own historical API), never through `backtest_runner.py`/`run_multi_backtest.py`.
- `confidence-stats`'s "fired" count is NOT proof an order executed — it increments before order placement is even attempted, with no rollback on failure. Verify real execution against the broker's own order/position data directly when it matters.
- Positions are broker-level (per symbol, not per strategy) — an exclusive ownership lock in `_close_position` prevents one strategy from closing another's position; only the opening strategy's own signal, the bracket order's own stop-loss/take-profit, or the news emergency stop (below) can close it.
- `_check_news_emergency_stop()` (2026-09-01) liquidates ANY open position, any strategy, the moment sentiment on that symbol falls to `config.NEWS_EMERGENCY_STOP_THRESHOLD` (-0.7, deliberately far more extreme than any entry-side sentiment threshold) — bypasses the ownership lock on purpose, same as the bracket order's own stop-loss/take-profit. Not the same thing as scalping's sentiment rider or mean_reversion's momentum filter — those gate NEW entries; this reacts to positions already open.
- Lumibot's own broker/data-source layer sometimes calls `get_last_price`/position-valuation directly on `self.broker`, bypassing this project's `Strategy`-subclass overrides (`get_historical_prices`/`get_last_price` caching in `strategy_manager.py`, the IB delayed-tick patch in `bot_runner.py`) entirely — confirmed live more than once (LOV, then recurring EURUSD/USDCZK "Unable to get data" noise). Not fixable from the `Strategy` subclass; needs a broker/data-source-level monkey-patch when it's worth fixing (see `bot_runner.py`'s existing patches for the established technique).
- This is a **paper-trading account**, but connected to **real broker infrastructure** (Alpaca or live IB Gateway, per `BROKER_PROVIDER`) — live bot start/stop, `.env`/credentials, and any destructive git operation require the user's explicit yes each time, even if a previous session was told the same thing.
