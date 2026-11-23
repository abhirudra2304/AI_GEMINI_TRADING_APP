#!/usr/bin/env python3
"""PreToolUse hook: block edits to strategy-parameter / scoring files unless approved.

Approval: the USER creates `.claude/APPROVE_STRATEGY_EDIT` (empty file = 1 edit;
a number inside = that many edits). Each allowed edit decrements/consumes it.
Claude must not create this file itself.

Covers Edit/Write/NotebookEdit by path, and Bash commands that look like writes
to a protected file (redirects, tee, sed -i, Set-Content, git checkout/restore,
python open(...,'w')). Bash detection is heuristic, not a sandbox.
Exit 2 = block (message goes to Claude), 0 = allow.
"""
import json
import os
import re
import sys

ROOT = os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
FLAG = os.path.join(ROOT, ".claude", "APPROVE_STRATEGY_EDIT")
PROTECTED = {
    "config.py", "discovery.py", "utils.py", "scanner_engine.py", "final_ranker.py",
    "market_context.py", "market_regime.py", "medium_rank.py", "signal_quality.py",
    "breakout_engine.py", "relative_strength.py", "sector_rotation.py",
    "institutional_flow.py", "extension_score.py",
    "ranker_config.yaml", "momentum_config.yaml", "universe_fitness_config.yaml",
    "volume_surge_config.yaml", "extension_config.yaml", "announcement_config.yaml",
}
WRITE_VERBS = re.compile(
    r"(>>?|\btee\b|\bsed\s+-i|\bperl\s+-i|set-content|add-content|out-file|\bgit\s+(checkout|restore|apply|stash|reset)\b"
    r"|\bcp\b|\bmv\b|\bcopy\b|\bmove\b|\bdel\b|\brm\b|open\([^)]*['\"][wa]|write_text|\.write\("
    r"|remove-item|rename-item|new-item|set-itemproperty|\bsc\b|\bni\b|\bri\b|\bren\b|\bpatch\b|shutil|os\.(replace|rename|remove)"
    r"|write_bytes|to_csv|writealltext|writeallbytes|\bgit\s+(revert|merge|pull|rebase|cherry-pick|am)\b)", re.I)
# Guard rails: never editable by Claude (the approval flag does NOT unlock these; the user edits them by hand)
SELF_NAMES = ("approve_strategy_edit", "settings.json", "settings.local.json")
HOOK_DIR = os.path.normcase(os.path.join(ROOT, ".claude", "hooks"))


def _is_guard(path: str) -> bool:
    p = os.path.normcase(os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path)))
    d = os.path.dirname(p)
    return d == HOOK_DIR or (d == os.path.join(ROOT, ".claude") and os.path.basename(p) in SELF_NAMES)


def _is_protected(path: str) -> bool:
    p = os.path.normcase(os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path)))
    return os.path.dirname(p) == ROOT and os.path.basename(p) in {n.lower() for n in PROTECTED} | PROTECTED


def _consume_approval() -> bool:
    if not os.path.exists(FLAG):
        return False
    try:
        raw = open(FLAG, encoding="utf-8").read().strip()
        left = int(raw) if raw.isdigit() else 1
        if left > 1:
            open(FLAG, "w", encoding="utf-8").write(str(left - 1))
        else:
            os.remove(FLAG)
        return True
    except OSError:
        return False


def _bash_hits(cmd: str):
    low = cmd.lower()
    if not WRITE_VERBS.search(cmd):
        return []
    return [n for n in PROTECTED if re.search(r"(?<![\w.])" + re.escape(n.lower()) + r"(?![\w])", low)]


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool, ti = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    hits = []
    if tool in ("Edit", "Write", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if path and _is_guard(path):
            sys.stderr.write("BLOCKED: .claude hooks/settings/approval flag are guard rails; only the user edits them by hand.\n")
            return 2
        if path and _is_protected(path):
            hits = [os.path.basename(path)]
    elif tool in ("Bash", "PowerShell"):
        cmd = ti.get("command", "")
        if WRITE_VERBS.search(cmd) and re.search(r"approve_strategy_edit|\.claude[\\/](hooks|settings)", cmd, re.I):
            sys.stderr.write("BLOCKED: command writes near .claude guard rails (hooks/settings/approval flag); only the user does that.\n")
            return 2
        hits = _bash_hits(cmd)
    if not hits:
        return 0
    if _consume_approval():
        return 0
    sys.stderr.write(
        "BLOCKED: strategy/scoring file(s) protected: " + ", ".join(sorted(set(hits))) + ".\n"
        "Ask the user for explicit approval; they approve by creating .claude/APPROVE_STRATEGY_EDIT "
        "(optionally containing the number of edits allowed). Do not create it yourself.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
