#!/usr/bin/env python3
"""
PreToolUse hook: blocks `main.py` invocations that would land on the
long-running interactive modes (btst/swing/gap/intraday), INCLUDING the
silent-default case.

Root cause this guards against (happened for real on 2026-09-02, left a
hung PID 25464 with no visible window): `main.py`'s CLI parser does
`mode = positionals[0] if positionals else "btst"`. An unrecognized flag
(e.g. `--help`, which main.py does not define) gets ignored by the parser
as noise, leaving `positionals` empty - so the command silently launches
`btst`, not an error. `btst`/`swing`/`gap`/`intraday` all route to
run_execution(), which ends in shutdown_manager.shutdown_event.wait() - it
blocks forever waiting for a Ctrl+C that never comes when launched
non-interactively.

Strategy: don't just pattern-match the four blocked words. First confirm
main.py is actually being EXECUTED (a python-interpreter token precedes it
in the same command segment - so `grep main.py`, `cat main.py` etc. are
left alone), then extract the actual positional mode argument the way
main.py's own parser would (first non-flag token after `main.py`, with
shell redirects/pipes/chaining stripped first), then:
  - main.py not actually being executed             -> allow
  - no positional token found                        -> BLOCK (silent-default case)
  - token is one of btst/swing/gap/intraday           -> BLOCK (explicit)
  - token is a recognized one-shot/other mode         -> allow
  - token is unrecognized (e.g. a typo)               -> BLOCK, conservatively -
    verify the mode name rather than risk it resolving to something unexpected

KNOWN_MODES is the full, exhaustive list of `mode ==` / `mode in [...]` /
`mode in (...)` branches in main.py's dispatch as of 2026-09-02 - re-check
it against main.py if this hook starts false-positiving on a new mode.

Exit code 2 blocks the tool call and returns this message to Claude.
Exit code 0 allows it.
"""
import json
import re
import sys

BLOCKED_MODES = {"btst", "swing", "gap", "intraday"}

KNOWN_MODES = BLOCKED_MODES | {
    "status", "check-sheet", "checksheet", "check_sheet",
    "download-constituents", "clean-constituents", "refresh-beta",
    "discover", "analyze", "cache", "profiler", "emfb", "momentum",
    "trigger-status", "history", "watchlist", "scorecard", "actionable",
    "extension", "news", "fitness", "expand", "surge", "gapscan",
    "reliability", "grind", "stockscan", "stock-scan", "brief",
    "decision-brief", "eod", "live", "report", "invalidate",
}

# main.py must actually be the script being run by a python interpreter in
# this command segment - not merely appear as a filename argument to grep,
# cat, an editor, etc. Lazily spans up to main.py within the same
# pipe/semicolon-delimited segment.
INVOCATION_RE = re.compile(
    # The (?<![\w.]) guard matters: without it, the bare `py` alternative
    # matches the ".py" INSIDE "main.py" itself, so a command that merely
    # mentions the filename twice (e.g. `grep -n 'main.py grind' main.py`)
    # looks like an interpreter invocation and gets blocked. Caught 2026-09-08
    # when this hook blocked a plain grep.
    r"(?<![\w.])(?:python3?(?:\.exe)?|py)\b[^|;&]*?main\.py\b(?P<rest>[^|;&]*)"
)

# Redirect clauses to strip before tokenizing, e.g. `2>&1`, `>>out.log`,
# `1>&2`, `<input.txt` - these are shell plumbing, not main.py arguments,
# but a bare trailing digit (a file descriptor number) immediately before
# `>`/`<` would otherwise be mistaken for a positional token.
REDIRECT_RE = re.compile(r"\d*(?:>>?|<)&?\d*\S*")


_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)


def extract_mode(command: str) -> tuple[bool, str | None]:
    """Returns (invokes_main_py, mode_or_None).

    Quoted spans are stripped first. A real invocation is unquoted; a command
    that merely CONTAINS the text (a grep pattern, an echo, a git commit
    message body in a heredoc) has it inside quotes. Without this the hook
    blocked `grep -n 'main.py grind' main.py` and even its own commit message -
    both caught 2026-09-08.
    """
    command = _QUOTED_SPAN_RE.sub(" ", command)
    match = INVOCATION_RE.search(command)
    if not match:
        return False, None

    rest = REDIRECT_RE.sub(" ", match.group("rest"))
    for tok in rest.split():
        if tok.startswith("-"):
            continue
        return True, tok
    return True, None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = payload.get("tool_input", {}).get("command", "")
    invokes_main_py, mode = extract_mode(command)
    if not invokes_main_py:
        return 0

    if mode is not None and mode in KNOWN_MODES and mode not in BLOCKED_MODES:
        return 0  # a valid, non-blocking one-shot mode - allow

    if mode is None:
        reason = (
            'no positional mode argument was found (only flags, or none at '
            'all) - main.py\'s parser defaults an empty positional list to '
            '"btst" SILENTLY, not an error'
        )
    elif mode in BLOCKED_MODES:
        reason = f"`{mode}` is one of the long-running interactive modes"
    else:
        reason = (
            f"`{mode}` is not a recognized main.py mode - if it's a typo, "
            f"main.py's own dispatch may also fail to match it; verify the "
            f"mode name before running"
        )

    print(
        f"Blocked: {reason}. main.py's btst/swing/gap/intraday modes route "
        f"to run_execution() -> shutdown_event.wait(), a long-running "
        f"interactive loop that hangs forever with no visible window when "
        f"launched non-interactively (this happened for real on 2026-09-02, "
        f"PID 25464, and had to be manually hunted down and killed).\n"
        f"Use the one-shot equivalent instead:\n"
        f"  - BTST/SWING data -> `stockscan` (pulls both via its own bridge)\n"
        f"  - EMFB           -> `emfb`\n"
        f"  - Other one-shot modes: momentum, gapscan, grind, reliability, "
        f"analyze, discover, status, cache, history, etc.\n"
        f"If you genuinely need the interactive loop (e.g. to hand off to "
        f"the user in their own terminal), tell the user to run it "
        f"themselves in a terminal they control, rather than launching it "
        f"from here.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
