"""mcp-fleet-common — the shared MCP-fleet package (Wave-6 G1, decision D16).

`pcai_utils/mcp_fleet_common/` is the source of truth; each consumer server
vendors a COPY of this directory at ``<app>/mcp_fleet_common/`` via
``pcai_utils/fleet_common_sync.sh`` (D16: new common code is NOT hardlink-meshed —
the existing mesh for current shared files is untouched). The copy carries a
``MANIFEST.sha256``; ``fleet_common_sync.sh --check`` detects drift.

Contents (see the README for the divergence catalog and adoption protocol):

* :mod:`.metrics`          — one parameterized implementation of the four
                             byte-identical ``mcp_metrics.py`` copies
                             (workbench/logsearch/prometheus/searxng).
* :mod:`.health`           — the shared ``/health``+``/healthz`` route factory
                             (byte-identical route pair in the same four
                             servers + applygate).
* :mod:`.namespace_policy` — the blocked-wins namespace policy shared by the
                             three implementations (logsearch/applygate/K8S-MCP).
* :mod:`.audit`            — applygate's hash-chained JSONL audit writer,
                             generalized (``prev_sha256`` + optional caller).

Design invariants (they are why this package is safe to adopt):

* Behavior parity first — everything extracted here is what the consumers do
  TODAY, byte-for-byte, except the ONE documented delta (unknown tool names
  normalize to the ``"unknown"`` metrics label, the B3-queued fix).
* Standard library only at import time; starlette/prometheus_client are
  imported lazily inside the handlers that need them, so a metrics failure
  can never break a tool call (the fleet import-guard pattern).
* No global state that one app could leak into another: every app binds its
  own configured objects (``Metrics(...)``, ``NamespacePolicy(...)``,
  ``HashChainedAuditLog(...)``) — the package holds no process-wide singletons.
"""

# SQLhandler vendors this package NESTED (src/sqlhandler/mcp_fleet_common/ —
# not at an app root like workbench), so the canonical top-level self-import is
# shimmed: try the fleet-standard binding first (root-vendored copies / an
# installed fleet package), fall back to this copy's qualified location. The
# vendored FILE CONTENTS stay byte-identical to the source of truth; this shim
# is re-applied after every fleet_common_sync.sh run (the sync test enforces it).
try:
    from mcp_fleet_common import audit, health, metrics, namespace_policy
except ModuleNotFoundError:  # nested vendoring (sqlhandler): import in place
    from . import audit, health, metrics, namespace_policy

__all__ = ["audit", "health", "metrics", "namespace_policy"]
__version__ = "0.1.0"
