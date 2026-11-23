"""Tests for .claude/hooks (subprocess, crafted stdin JSON). Order-call strings are
built by concatenation so this file does not trip its own guard."""
import json
import os
import subprocess
import sys

import pytest

HOOKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "hooks")
FLAG = os.path.join(os.path.dirname(HOOKS), "APPROVE_STRATEGY_EDIT")
PO = "pla" + "ceOrder"


def run(hook, payload):
    r = subprocess.run([sys.executable, os.path.join(HOOKS, hook)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr


@pytest.fixture(autouse=True)
def no_flag():
    if os.path.exists(FLAG):
        pytest.skip("approval flag present; refusing to touch it")
    yield
    if os.path.exists(FLAG):
        os.remove(FLAG)


def test_protected_edit_blocked():
    for f in ("config.py", "discovery.py", "ranker_config.yaml"):
        code, err = run("protect_strategy_files.py", {"tool_name": "Edit", "tool_input": {"file_path": "C:/AI_GEMINI_TRADING_APP/" + f}})
        assert code == 2 and "APPROVE_STRATEGY_EDIT" in err


def test_unprotected_edit_allowed():
    code, _ = run("protect_strategy_files.py", {"tool_name": "Write", "tool_input": {"file_path": "C:/AI_GEMINI_TRADING_APP/explain_rank.py"}})
    assert code == 0


def test_same_name_in_subdir_allowed():
    code, _ = run("protect_strategy_files.py", {"tool_name": "Edit", "tool_input": {"file_path": "C:/AI_GEMINI_TRADING_APP/scratch/config.py"}})
    assert code == 0


def test_approval_flag_is_one_shot():
    open(FLAG, "w").close()
    p = {"tool_name": "Edit", "tool_input": {"file_path": "C:/AI_GEMINI_TRADING_APP/config.py"}}
    assert run("protect_strategy_files.py", p)[0] == 0
    assert not os.path.exists(FLAG)
    assert run("protect_strategy_files.py", p)[0] == 2


def test_bash_write_blocked_read_allowed():
    w = {"tool_name": "Bash", "tool_input": {"command": "sed -i s/1/2/ config.py"}}
    r = {"tool_name": "Bash", "tool_input": {"command": "cat config.py | head -5"}}
    assert run("protect_strategy_files.py", w)[0] == 2
    assert run("protect_strategy_files.py", r)[0] == 0


def test_orders_blocked():
    for tool, ti in (("Bash", {"command": "python -c \"api." + PO + "(x)\""}),
                     ("Write", {"file_path": "x.py", "content": "client.modify" + "Order(1)"}),
                     ("Edit", {"file_path": "x.py", "new_string": "/rest/secure/angel" + "broking/order/v1/" + "pla" + "ceOrder"})):
        code, err = run("block_orders.py", {"tool_name": tool, "tool_input": ti})
        assert code == 2 and "BLOCKED" in err


def test_orders_search_and_normal_allowed():
    assert run("block_orders.py", {"tool_name": "Grep", "tool_input": {"pattern": PO}})[0] == 0
    assert run("block_orders.py", {"tool_name": "Bash", "tool_input": {"command": "grep -rn " + PO + " *.py"}})[0] == 0
    assert run("block_orders.py", {"tool_name": "Bash", "tool_input": {"command": "python main.py stockscan"}})[0] == 0
    assert run("block_orders.py", {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "new_string": "x = 1"}})[0] == 0


def test_guard_rails_not_editable_even_with_flag():
    open(FLAG, "w").close()
    for f in (".claude/APPROVE_STRATEGY_EDIT", ".claude/settings.json", ".claude/hooks/block_orders.py"):
        code, err = run("protect_strategy_files.py", {"tool_name": "Write", "tool_input": {"file_path": "C:/AI_GEMINI_TRADING_APP/" + f}})
        assert code == 2 and "guard" in err
    assert os.path.exists(FLAG)  # not consumed


def test_bash_guard_and_extra_verbs():
    for c in ("echo x > .claude/APPROVE_STRATEGY_EDIT", "Remove-Item .claude/hooks/block_orders.py", "Remove-Item " + "con" + "fig.py"):
        assert run("protect_strategy_files.py", {"tool_name": "PowerShell", "tool_input": {"command": c}})[0] == 2


def test_orders_newline_after_grep_blocked():
    cmd = "grep x f\npython -c \"api." + PO + "(1)\""
    assert run("block_orders.py", {"tool_name": "Bash", "tool_input": {"command": cmd}})[0] == 2


def test_run_tests_skips_non_py():
    assert run("run_tests.py", {"tool_name": "Edit", "tool_input": {"file_path": "notes.md"}})[0] == 0
