# Rules for Antigravity / Gemini assistant (mirrors CLAUDE.md guards)

Role: low-cost helper so Claude tokens are saved. Default mode is READ-ONLY analysis.

## Never
- Never place, modify or suggest placing orders or GTTs; never call any broker order endpoint.
- Never edit scoring/strategy/param files: config*, discovery.py, utils.py, scanner_engine.py, final_ranker.py, market_*.py, *_config.yaml, emfb.py, decision_brief.py, data_broker.py, main.py.
- Never edit anything under .claude/ or scripts/*.ps1 scheduled wrappers without the user asking.
- Never read, print or copy .env, tokens, API keys or session files. Never put credentials in logs or commits.
- Never run main.py btst / swing / gap / intraday (they hang). Use `main.py stockscan`, `emfb`, `brief` only if asked.
- Never delete or overwrite report/journal CSVs: they are the scorecard data.
- Never commit or push unless asked.

## Good uses (cheap work)
- Summarize reports/*.md, logs/*.log, journal/*, reports/*_watch_*.md (look for *_SIGNAL files first).
- Explain script output; draft new standalone helper scripts in a new file; first-pass code review notes.
- Research/backtest drafts that write results to a new file under reports/ or scratch/.

## Conventions
- Python: `.venv\Scripts\python.exe`; pandas is already imported as pd in analysis snippets.
- Output: terse findings only, no filler. Show diffs, not whole files.
- Signals are candidates to review, not buy/sell advice. Never claim a signal a tool did not print.
- Market data: bars fetched intraday are provisional until the 20:15 official-close overlay.
- Project history: no strategy has a proven edge net of costs (see reports/validation_*.md); do not tune to backtests.

## Escalate to Claude (tell the user) when
- A change touches scoring/strategy logic, data integrity, credentials, or the scheduled tasks.
- Results contradict prior validated findings, or tests fail after an edit.
