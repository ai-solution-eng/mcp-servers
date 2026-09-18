"""DNS-1123 validation tests — namespace and resource names are validated as
RFC1123 DNS labels/subdomains BEFORE they flow into the DynamicClient URL
path (fleet-audit §3.3: "namespace/name flow raw into the DynamicClient URL
path"). Malformed input → a clear, self-describing validation refusal; every
previously-valid flow is unchanged.

Namespace = RFC1123 DNS LABEL (lowercase alnum + '-', ≤63).
Names     = RFC1123 DNS SUBDOMAIN (dot-separated labels, ≤253) — the
            Kubernetes object-name rule for every kind on the allowlist,
            so dotted names like "my.team.config" keep working.

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_dns_validation.py -v
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
    "APPLYGATE_BLOCKED_NAMESPACES",
    "APPLYGATE_ALLOWED_KINDS",
    "APPLYGATE_AUDIT_FILE",
    "APPLYGATE_UNPLANNED_APPLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYGATE_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    server._caller_context.set(None)  # no caller leakage between tests
    # Gate-isolation fixture (see tests/test_applygate.py): these tests
    # exercise DNS validation, not the D11 binding.
    monkeypatch.setenv("APPLYGATE_UNPLANNED_APPLY", "allow")
    monkeypatch.setenv("APPLYGATE_ALLOWED_NAMESPACES", "*")


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


def install_delete_spy(monkeypatch):
    spy = _SeamSpy(("namespace", "kind", "name"), result={"response": {}})
    monkeypatch.setattr(server, "_delete", spy)
    return spy


def install_status_fake(monkeypatch):
    spy = _SeamSpy(("namespace", "kind", "name"), result={"status": {"phase": "Running"}})
    monkeypatch.setattr(server, "_get_status", spy)
    return spy


def doc(name="app-config", ns=None):
    d = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}}
    if ns:
        d["metadata"]["namespace"] = ns
    return d


def manifest(*docs):
    return "\n---\n".join(yaml.safe_dump(d) for d in docs)


def parse(out: str) -> dict:
    return json.loads(out)


VALID_NAMESPACES = ("team-a", "platform", "a", "0abc", "team-1-2-3", "x" * 63)
# A 253-char name is legal ONLY as dot-separated ≤63-char labels (RFC1123
# subdomain — same rule the k8s API applies): four 62-63-char labels + dots.
MAX_LEGAL_NAME = ".".join(["x" * 63, "x" * 63, "x" * 63, "x" * 61])
assert len(MAX_LEGAL_NAME) == 253
VALID_NAMES = ("app-config", "my.team.config", "a", "svc-1.web.internal", "x" * 63, MAX_LEGAL_NAME)
INVALID_NAMESPACES = (
    "Bad_Name",  # underscore
    "-lead",  # leading hyphen
    "trail-",  # trailing hyphen
    "UPPER",  # uppercase
    "double..dot",  # (subdomain-style failure) empty label
    "x" * 64,  # label cap is 63
    "sp ace",  # space
    "ns/slash",  # path smuggling
    "ns?query",  # query smuggling
    "",  # empty
)
INVALID_NAMES = (
    "Bad_Name",
    "-lead",
    "trail-",
    "UPPER",
    "..",
    "dot..dot",
    "x" * 254,  # subdomain ceiling is 253
    "x" * 64,  # a single label is still a label (≤63)
    "na me",
    "name/slash",
    "name$",
    "",
)


# ---------------------------------------------------------------------------
# valid inputs pass and reach the seams unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("namespace", VALID_NAMESPACES)
def test_valid_namespaces_pass(monkeypatch, namespace):
    spy = install_apply_spy(monkeypatch)
    plan = parse(run(server.plan_apply(namespace=namespace, manifest=manifest(doc()))))
    assert plan["ok"] is True and "refused" not in plan
    assert [c["namespace"] for c in spy.calls] == [namespace]


@pytest.mark.parametrize("name", VALID_NAMES)
def test_valid_names_pass_including_dotted_subdomains(monkeypatch, name):
    """Dotted RFC1123 subdomains are LEGAL object names — they must keep
    flowing through (this is why names are validated as subdomains, not
    bare labels)."""
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.apply_manifest(namespace="team-a", manifest=manifest(doc(name=name)), confirm_apply=True)))
    assert out["ok"] is True, out
    assert spy.calls[0]["doc"]["metadata"]["name"] == name


# ---------------------------------------------------------------------------
# invalid inputs → clear validation refusals, seams untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("namespace", INVALID_NAMESPACES)
def test_invalid_namespaces_refused_with_rfc1123_message(monkeypatch, namespace):
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace=namespace, manifest=manifest(doc()))))
    assert out["refused"] is True and out["ok"] is False
    assert "RFC1123" in out["error"]
    if namespace:
        assert "URL path" in out["error"]  # refused BEFORE reaching the API path
    else:
        assert "required" in out["error"]
    assert spy.calls == []


@pytest.mark.parametrize("namespace", INVALID_NAMESPACES)
def test_invalid_namespaces_refused_on_apply_delete_status(monkeypatch, namespace):
    install_apply_spy(monkeypatch)
    del_spy = install_delete_spy(monkeypatch)
    status_spy = install_status_fake(monkeypatch)
    out = parse(run(server.apply_manifest(namespace=namespace, manifest=manifest(doc()), confirm_apply=True)))
    assert out["refused"] is True and "RFC1123" in out["error"]
    out = parse(run(server.delete_resource(namespace=namespace, kind="ConfigMap", name="x", confirm_delete=True)))
    assert out["refused"] is True and "RFC1123" in out["error"]
    out = parse(run(server.get_resource_status(namespace=namespace, kind="ConfigMap", name="x")))
    assert out["refused"] is True and "RFC1123" in out["error"]
    assert del_spy.calls == [] and status_spy.calls == []


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_invalid_resource_names_refused_with_rfc1123_message(monkeypatch, name):
    """The mission matrix: Bad_Name, -lead, trail-, 254-char, '..', uppercase."""
    del_spy = install_delete_spy(monkeypatch)
    status_spy = install_status_fake(monkeypatch)
    out = parse(run(server.delete_resource(namespace="team-a", kind="ConfigMap", name=name, confirm_delete=True)))
    assert out["refused"] is True
    assert "RFC1123" in out["error"] and "resource name" in out["error"]
    out = parse(run(server.get_resource_status(namespace="team-a", kind="ConfigMap", name=name)))
    assert out["refused"] is True and "RFC1123" in out["error"]
    assert del_spy.calls == [] and status_spy.calls == []


@pytest.mark.parametrize("name", ["Bad_Name", "..", "x" * 254, "-lead", "trail-"])
def test_invalid_doc_names_refused_at_manifest_hygiene(monkeypatch, name):
    """Doc-level metadata.name gets the same gate inside _parse_manifest —
    a manifest is refused wholesale before any doc reaches the seam."""
    spy = install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace="team-a", manifest=manifest(doc(name=name)))))
    assert out["refused"] is True and "manifest rejected" in out["error"]
    assert "RFC1123" in out["error"]
    assert spy.calls == []


def test_validation_precedes_the_namespace_policy(monkeypatch):
    """A malformed namespace gets the DNS-1123 refusal even under
    default-deny (the validation error is the MORE precise one)."""
    monkeypatch.delenv("APPLYGATE_ALLOWED_NAMESPACES", raising=False)
    install_apply_spy(monkeypatch)
    out = parse(run(server.plan_apply(namespace="Bad_NS", manifest=manifest(doc()))))
    assert out["refused"] is True and "RFC1123" in out["error"]
    assert "DEFAULT-DENY" not in out["error"]


def test_validation_refusal_is_audit_logged(tmp_path):
    run(server.delete_resource(namespace="Bad_NS", kind="ConfigMap", name="x", confirm_delete=True))
    import json as _json

    lines = [_json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0]["outcome"] == "refused"
