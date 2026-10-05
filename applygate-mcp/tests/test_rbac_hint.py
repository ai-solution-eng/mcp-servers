"""RBAC ↔ registry mismatch hint tests — the pure 403-explainer.

The kind registry (server._KIND_REGISTRY) is the ADMISSION vocabulary: it
admits more kinds than the chart's Role (helm/templates/rbac.yaml mirrors
only the default allowlist) and the one-time bootstrap manifest grants. An
operator can therefore allowlist a registry kind whose resource the
ServiceAccount lacks — the tool's policy passes and the API answers 403.
`rbac_hint_for(kind, verb)` turns that 403 into an operator-actionable hint
pointing at the exact manifests to extend — and, critically, at the
invariant that the TOOL never extends RBAC (this file pins the text; the
hint function is pure and changes no cluster state).

Run:
    cd /home/andrew/Code/HPE/mcp_servers/applygate_mcp && \
    /home/andrew/Code/HPE/SQLhandler/.venv312/bin/python -m pytest tests/test_rbac_hint.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server

# The exact resource surface the chart Role (and the bootstrap manifest's
# per-namespace Roles) grant — the kinds a 403 should NEVER be possible for
# at the RBAC layer, so the hint stays None for them.
_CHART_GRANTED_KINDS = (
    "ConfigMap",
    "Service",
    "ServiceAccount",
    "Pod",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Ingress",
    "NetworkPolicy",
    "PodDisruptionBudget",
    "HorizontalPodAutoscaler",
)


def test_hint_for_registry_kind_missing_from_the_chart_role():
    """A registry-admitted kind the chart Role does not grant → actionable hint."""
    hint = server.rbac_hint_for("PersistentVolumeClaim", "access")
    assert hint is not None
    assert "persistentvolumeclaims" in hint  # the plural to add
    assert "does not imply cluster RBAC grants it" in hint
    assert "403" in hint
    assert "rbac.yaml" in hint  # the chart Role to extend
    assert "rbac-bootstrap" in hint  # the bootstrap manifest for other namespaces
    assert "never extends RBAC" in hint  # the tool-side invariant


def test_hint_is_none_for_every_chart_granted_kind():
    for kind in _CHART_GRANTED_KINDS:
        assert server.rbac_hint_for(kind, "access") is None, kind


def test_hint_names_the_api_group_of_the_kind():
    hint = server.rbac_hint_for("Lease", "access")
    assert hint is not None
    assert "leases" in hint
    assert "coordination.k8s.io/v1" in hint
    hint = server.rbac_hint_for("Role", "access")
    assert hint is not None
    assert "rbac.authorization.k8s.io/v1" in hint


def test_hint_is_none_for_gate_refused_kinds():
    """Secret and cluster-scoped kinds never reach an API call — they are
    refused at the gate — so no RBAC hint exists for them."""
    assert server.rbac_hint_for("Secret", "access") is None
    assert server.rbac_hint_for("Namespace", "access") is None
    assert server.rbac_hint_for("ClusterRole", "access") is None


def test_hint_is_none_for_unregistered_kind():
    assert server.rbac_hint_for("NotARealKind", "access") is None


def test_hint_is_pure_deterministic_text():
    """Same inputs, same output; nothing global is touched."""
    a = server.rbac_hint_for("ResourceQuota", "access")
    b = server.rbac_hint_for("ResourceQuota", "access")
    assert a is not None and a == b
