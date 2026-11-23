# Token & Output Rules
- NO CONVERSATIONAL FILLER: Do not say "Here is the code" or "I have updated the file."
- NO FILE REWRITES: Only output unified diffs or specific code blocks for changed lines.
- HIGH CONTEXT COST: Do not summarize code unless I explicitly ask.
- TRADING LOGIC: When analyzing scripts, assume 'pandas' is already imported as 'pd'.

# Workflow commands (Stage 1, 2026-11-23)
- `/premarket` -> briefs/YYYY-MM-DD_premarket.md (uses gapscan + brief; raw btst/swing/gap/intraday hang, hook-blocked).
- `/eod` -> main.py eod, then `journal_signals.py` appends resolved picks to journal/signals.csv (daily bars; stop wins ties).
- `/explain SYM [STRAT]` -> `explain_rank.py`, reads discovery_cache_*.pkl, no API calls.
- *.csv reads are denied in settings; use script output or python -c.

# Subagents (Stage 2, 2026-11-23) in .claude/agents/
- news-checker (WebSearch, Read, Grep): red-flag table per symbol.
- risk-reviewer (Bash, Read, Grep): stop/ATR, liquidity, size, sector concentration.
- code-reviewer (Read, Grep, Glob, Bash=git/pytest only): review scoring diffs before use.
- Agents load at session start; restart the session before spawning new ones.

# Hooks (Stage 3, 2026-11-23) in .claude/hooks/, wired in .claude/settings.json
- protect_strategy_files.py: blocks edits to scoring/param files (config, discovery, utils, scanner_engine, final_ranker, market_*, *_config.yaml, etc). Only the USER approves, by creating .claude/APPROVE_STRATEGY_EDIT (empty = 1 edit; number = N edits). Claude never creates it.
- run_tests.py: pytest -q -x after any .py edit; failure tail returned to Claude.
- block_orders.py: blocks any order/GTT call or broker order endpoint in any tool input (grep/Read exempt). Tests build such strings by concatenation.
- Stage 4 (2026-11-23): `validate_strategies.py` -> reports/validation_<date>.md (itemised fees+slippage, Nifty EMA20/50 regime, bias audit; ~5 min run). Result: every strategy net-negative except SWING gross-positive (+0.05% net, t-stat unreliable); window only 2026-05-14..11-19, hindsight universe, no splits adjustment. No edge shown; re-run weekly, don't tune to it.
- Stage 5 (2026-11-23): `scripts/run_claude_headless.ps1 -Mode premarket|eod [-CheckOnly]` -> `claude -p`, log logs/claude_<mode>_<date>.log; holiday gate, main.pid wait, premarket skips CAS window, 25 min timeout; hooks stay active (never --bare). eod mode passes `skip-scan` (run_eod.ps1 already ran main.py eod). Tasks NOT modified. Headless auth: 11-23 test failed "OAuth session expired" -> a scheduled S4U task needs `claude setup-token` + CLAUDE_CODE_OAUTH_TOKEN (user sets it; Claude never handles it).
- Cross-audit 2026-11-23 (code-reviewer agent) fixed: .claude/hooks + settings.json + approval flag now unwritable by Claude even with the flag; more write verbs; newline bypass in block_orders; journal NO_FILL only after full window, 30-day lookback, only bars < asof; maxDD starts from 0; headless tool scoping + tree-kill + exit codes. NOT done (user policy): protect emfb/decision_brief/data_broker/main etc.; block_blocking_scan_modes only matches Bash; hooks fail open on bad stdin.
- Tests: test_hooks.py. Bash-write detection is heuristic (false positives possible: a command naming a protected file next to a redirect); use Edit/Write for docs.
