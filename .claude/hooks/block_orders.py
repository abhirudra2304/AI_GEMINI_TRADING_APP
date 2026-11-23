#!/usr/bin/env python3
"""PreToolUse hook (all tools): never allow broker order placement.

Blocks any tool call whose input contains an order-placement / modification /
cancellation / GTT call or broker order endpoint. Read/search tools (Read, Grep,
Glob) and Bash commands that only search (grep/rg/findstr/Select-String) are
exempt so the codebase can still be audited for these names.
Patterns are written with character classes so this file does not match itself.
Exit 2 = block, 0 = allow.
"""
import json
import re
import sys

PATTERNS = re.compile(
    r"(pl[a]ce_?[o]rder|m[o]dify_?[o]rder|c[a]ncel_?[o]rder|[g]tt_?(create|modify|cancel)"
    r"|cre[a]te_?[r]ule|/rest/secure/[a]ngelbroking/(?:[o]rder|[g]tt)|[o]rder/v\d/(?:pl[a]ce|m[o]dify|c[a]ncel)"
    r"|k[i]te\.(?:pl[a]ce|m[o]dify|c[a]ncel)|\bsell[_ ]?[o]rder\b|\bbuy[_ ]?[o]rder\b)", re.I)
EXEMPT_TOOLS = {"Read", "Grep", "Glob", "ReadNotifications"}
SEARCH_CMD = re.compile(r"^\s*(git\s+grep|grep|egrep|rg|findstr|select-string|sls)\b", re.I)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool, ti = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    if tool in EXEMPT_TOOLS:
        return 0
    if tool in ("Bash", "PowerShell") and SEARCH_CMD.match(ti.get("command", "")) and not re.search(r"[;&|\n`]\s*(?!grep|rg|head|tail|sort|wc)\w|\$\(", ti.get("command", "")):
        return 0
    blob = json.dumps(ti)
    m = PATTERNS.search(blob)
    if not m:
        return 0
    sys.stderr.write(
        "BLOCKED: order-placement call/endpoint detected (" + m.group(0) + "). "
        "This project is read-only for broker access: data fetches only, no orders.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
