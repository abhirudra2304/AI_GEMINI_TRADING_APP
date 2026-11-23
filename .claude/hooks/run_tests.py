#!/usr/bin/env python3
"""PostToolUse hook: run the pytest suite after any .py edit.

Silent on pass. On failure exits 2 so pytest's tail is fed back to Claude.
Skipped for files under .claude/ is NOT done on purpose: hook code is tested too.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    ti = data.get("tool_input", {}) or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if not path.lower().endswith(".py"):
        return 0
    exe = PY if os.path.exists(PY) else sys.executable
    try:
        r = subprocess.run([exe, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        sys.stderr.write("TESTS: pytest timed out after 240s after editing " + os.path.basename(path) + "\n")
        return 2
    if r.returncode == 0:
        return 0
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
    sys.stderr.write("TESTS FAILED after editing " + os.path.basename(path) + ":\n" + tail + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
