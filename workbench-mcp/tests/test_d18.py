"""Fleet decision D18 (2026-09-13, operator-ratified): NO python execution in
the workbench DEFAULT allowlist.

python3/pip/pip3 removed from _DEFAULT_ALLOW and from the chart's
execAllowlist default: an allow-listed interpreter can compute paths at
runtime and sidestep the argv-level D7 confinement (split-string paths never
appear in argv — proven by the Wave-3 verifier), and pip executes python
code (setup.py).  The model-facing note lives in run_command's description.
Operators re-add via WORKBENCH_EXEC_ALLOWLIST / values.execAllowlist
explicitly when their threat model accepts the documented residual.
(test helpers ``call``/``root`` mirror tests/test_workbench.py.)
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re

import pytest

import server
from server import WorkbenchError


def call(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROOT", str(tmp_path / "data"))
    return tmp_path / "data"


def test_default_allowlist_excludes_python_execution():
    names = {b.strip() for b in server._DEFAULT_ALLOW.split(",")}
    assert "python3" not in names, "D18: python3 must not ship in the default allowlist"
    assert "python" not in names
    assert "pip" not in names and "pip3" not in names
    for expected in ("ls", "cat", "grep", "find", "tar", "git", "sort", "uniq"):
        assert expected in names
    assert "curl" in server._DEFAULT_DENY


def test_python3_refused_under_default(root, monkeypatch):
    """End-to-end: under the DEFAULT (no env override), python3 is refused."""
    monkeypatch.delenv("WORKBENCH_EXEC_ALLOWLIST", raising=False)
    call(server.workspace_create, "d18ws")
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "d18ws", ["python3", "-c", "print('hello')"])


def test_pip_refused_under_default(root, monkeypatch):
    monkeypatch.delenv("WORKBENCH_EXEC_ALLOWLIST", raising=False)
    call(server.workspace_create, "d18pip")
    with pytest.raises(WorkbenchError, match="not in the allowlist"):
        call(server.run_command, "d18pip", ["pip", "install", "requests"])


def test_escape_hatch_restores_python3(root, monkeypatch):
    """The operator's opt-in re-adds python3 — the hatch is real."""
    monkeypatch.setenv("WORKBENCH_EXEC_ALLOWLIST", "python3,ls")
    call(server.workspace_create, "d18opt")
    out = json.loads(call(server.run_command, "d18opt", ["python3", "-c", "print('d18-ok')"]))
    assert "d18-ok" in out["stdout"]


def test_run_command_description_carries_the_model_note():
    """The policy is visible to MODELS: run_command's served description
    (the MCP layer surfaces the tool docstring) states the D18 policy."""
    doc = server.run_command.__doc__ or ""
    assert "D18" in doc
    assert "NO interpreters" in doc
    assert "WORKBENCH_EXEC_ALLOWLIST" in doc
    assert "not in the allowlist" in doc


def test_chart_default_matches_server_default():
    """The chart's execAllowlist default and the server's must agree (D18)."""
    vals = pathlib.Path(__file__).resolve().parent.parent / "helm" / "values.yaml"
    m = re.search(r'execAllowlist:\s*"([^"]+)"', vals.read_text())
    assert m, "execAllowlist default missing from values.yaml"
    chart_names = {b.strip() for b in m.group(1).split(",")}
    server_names = {b.strip() for b in server._DEFAULT_ALLOW.split(",")}
    assert "python3" not in chart_names and "pip" not in chart_names and "pip3" not in chart_names
    assert chart_names == server_names, "chart and server defaults diverged"
