"""Alias-bomb-safe YAML loading tests.

The manifest parser uses an alias-limited SafeLoader: caps on alias
resolutions and composed nodes, plus a post-parse EXPANSION BUDGET that
counts the fully-expanded tree (the number the loader caps alone cannot
bound, because aliases share one node object) and rejects self-referential
structures (YAML quines). Classic billion-laughs and quines are refused with
a clear _ManifestError; SANE manifests — including ones that legitimately
use anchors/aliases — parse to EXACTLY the same documents yaml.safe_load_all
yields, so the dry-run seam sees identical bytes.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_yaml_loader.py -v
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

import server

ENV_VARS = (
    "APPLYGATE_ALLOWED_NAMESPACES",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_UNPLANNED_APPLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._caller_context.set(None)  # no caller leakage between tests
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "allow")  # gate isolation (see test_applygate.py)
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "team-a")


def run(coro):
    return asyncio.run(coro)


class _SeamSpy:
    def __init__(self, names, result=None):
        self.names = names
        self.calls = []
        self.result = result if result is not None else {"metadata": {"resourceVersion": "1"}}

    def __call__(self, *args, **kwargs):
        record = dict(zip(self.names, args))
        record.update(kwargs)
        self.calls.append(record)
        return self.result


def install_apply_spy(monkeypatch):
    spy = _SeamSpy(("namespace", "doc", "dry_run"))
    monkeypatch.setattr(server, "_ssa_apply", spy)
    return spy


def doc(name="app-config", **extra):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, **extra}


def manifest(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs)


def parse(out: str) -> dict:
    return json.loads(out)


def plan(monkeypatch, text):
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace="team-a", manifest=text)))
    return out, spy


# ---------------------------------------------------------------------------
# billion laughs — the classic 9×9 alias pyramid
# ---------------------------------------------------------------------------


def billion_laughs(width=9, depth=9):
    lines = [f"a0: &a0 [{','.join(['x'] * width)}]"]
    for i in range(1, depth):
        lines.append(f"a{i}: &a{i} [{','.join([f'*a{i - 1}'] * width)}]")
    return (
        "\n".join(lines) + f"\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\ndata:\n  bomb: *a{depth - 1}\n"
    )


def test_classic_billion_laughs_refused_with_clear_error(monkeypatch):
    bomb = billion_laughs()
    # Sanity: plain PyYAML parses it happily (the bomb hides below the parse).
    parsed = list(yaml.safe_load_all(bomb))
    assert isinstance(parsed[0]["data"]["bomb"], list)
    out, spy = plan(monkeypatch, bomb)
    assert out["refused"] is True
    assert "manifest rejected" in out["error"]
    assert ("expands to more than" in out["error"]) or ("alias" in out["error"])
    assert spy.calls == []  # the bomb never reaches the dry-run seam


def test_smaller_bomb_still_refused_and_faster_than_the_budget(monkeypatch):
    """Even a modest 6-wide × 7-deep pyramid (~280k expanded nodes) is
    caught: the refusal is budget-based, not size-luck."""
    out, spy = plan(monkeypatch, billion_laughs(width=6, depth=7))
    assert out["refused"] is True
    assert spy.calls == []


# ---------------------------------------------------------------------------
# quines / self-referential structures
# ---------------------------------------------------------------------------


def test_classic_yaml_quine_refused_with_dedicated_error(monkeypatch):
    """`&d [*d]` parses into a list that contains itself — refused with the
    dedicated recursive-structure message (a budget refusal would be the
    wrong diagnosis)."""
    quine = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: q\ndata: &d [*d]\n"
    out, spy = plan(monkeypatch, quine)
    assert out["refused"] is True
    assert "self-referential" in out["error"] and "quine" in out["error"]
    assert spy.calls == []


def test_nested_quine_refused(monkeypatch):
    """An anchor transitively containing its own alias (the two-level
    classic quine) — same refusal, same clear error."""
    quine = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: q\na: &a\n  b: &b [*a]\n"
    out, spy = plan(monkeypatch, quine)
    assert out["refused"] is True
    assert "self-referential" in out["error"]
    assert spy.calls == []


# ---------------------------------------------------------------------------
# other parser-level caps
# ---------------------------------------------------------------------------


def test_alias_count_cap_refuses_wide_alias_use(monkeypatch):
    """More than _MAX_ALIAS_RESOLITIONS alias resolutions are refused by the
    LOADER cap (before any expansion)."""
    wide = "base: &b [x]\nitems:\n"
    wide += "\n".join("  - *b" for _ in range(server._MAX_ALIAS_RESOLITIONS + 1))
    wide += "\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n"
    out, spy = plan(monkeypatch, wide)
    assert out["refused"] is True
    assert f"more than {server._MAX_ALIAS_RESOLITIONS} YAML aliases" in out["error"]
    assert spy.calls == []


def test_overly_deep_nesting_is_a_clear_error_not_a_crash(monkeypatch):
    deep = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: d\ndata:\n" + "  " * 1 + "k: " + "[" * 5000 + "]" * 5000
    out, _ = plan(monkeypatch, deep)
    assert out["refused"] is True
    assert "manifest rejected" in out["error"]
    assert ("not valid YAML" in out["error"]) or ("nests too deeply" in out["error"]) or ("nodes" in out["error"])


# ---------------------------------------------------------------------------
# sane manifests — including legitimate anchors — are unaffected
# ---------------------------------------------------------------------------


def test_sane_manifest_with_anchors_aliases_parses_identically(monkeypatch):
    """THE compat contract: the alias-limited loader yields the SAME
    documents yaml.safe_load_all does — same data AND preserved alias
    semantics (aliases resolve to one shared object) — so the dry-run seam
    sees the same manifest."""
    text = (
        "defaults: &defaults\n  ttl: 30\n  mode: safe\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm-with-aliases\n"
        "top: *defaults\nother: *defaults\n"
    )
    out, spy = plan(monkeypatch, text)
    assert out["ok"] is True and out["dry_run"] is True
    expected = next(iter(yaml.safe_load_all(text)))
    assert spy.calls, "seam must be reached for a sane aliased manifest"
    got = spy.calls[0]["doc"]
    assert got == expected  # same data...
    assert got["top"] is got["other"]  # ...same object graph (alias sharing preserved)


def test_multi_doc_manifest_plans_per_doc(monkeypatch):
    # Anchors do NOT cross '---' document boundaries — each doc is
    # self-contained here; the separator is what makes it multi-doc.
    doc_a = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\ndata:\n  x: &x hello\n  y: *x"
    doc_b = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\ndata:\n  z: plain"
    text = f"{doc_a}\n---\n{doc_b}"
    out, spy = plan(monkeypatch, text)
    assert out["summary"] == {"total": 2, "ok": 2, "failed": 0}
    assert [c["doc"]["metadata"]["name"] for c in spy.calls] == ["a", "b"]


def test_loader_caps_are_generous_for_real_manifests(monkeypatch):
    """A large-but-legitimate manifest (8 docs, real structure) plans clean —
    the caps sit orders of magnitude above honest use."""
    out, spy = plan(
        monkeypatch, manifest(*[doc(name=f"cm-{i}", data={f"k{j}": "v" for j in range(20)}) for i in range(8)])
    )
    assert out["summary"] == {"total": 8, "ok": 8, "failed": 0}
    assert len(spy.calls) == 8


def test_expanded_node_count_measures_the_expanded_tree():
    """Unit check of the budget walker: shared DAG counted expanded, cycles
    rejected."""
    shared = [1, 2, 3]
    dag = {"a": shared, "b": shared, "c": shared}
    # Expanded: dict + 3 keys + 3×(list + 3 items) = 1 + 3 + 3×4 = 16
    assert server._expanded_node_count(dag, budget=1000) == 16
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(server._ManifestError) as exc:
        server._expanded_node_count(cyclic, budget=1000)
    assert "self-referential" in str(exc.value)
    with pytest.raises(server._ManifestError) as exc:
        server._expanded_node_count(dag, budget=10)
    assert "expands to more than" in str(exc.value)
