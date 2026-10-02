#!/usr/bin/env python3
"""Mint one SQLhandler /mcp API key and print its policy wiring.

The admin workflow for the dataset-ACL rollout (documentation/DEPLOYMENT.md
"Dataset ACLs — mint + grants"):

  1. the script prints a fresh key (``secrets.token_urlsafe(32)``) and its
     FINGERPRINT — computed by the SAME function the server's auth layer uses
     (``sqlhandler.mcp_fleet_common.audit.key_fingerprint``: ``"sha256:"`` +
     the first 12 hex chars of sha256(key).utf-8 — verified by
     tests/test_mint_key.py against the real import),
  2. the operator APPENDS the key to the comma-separated ``SQLHANDLER_API_KEYS``
     Secret value (rotation story: append → move clients → drop the old; the
     env is re-read per request, so no restart),
  3. the operator merges the printed ``assignments`` entry — keyed by the
     fingerprint, ``key:sha256:<12hex>``-style — into the policy file's
     ``datasets.assignments`` object (ConfigMap edit → mtime hot reload, no
     restart).

Discipline: stdlib only, NO network, NO filesystem writes, and the raw key is
printed to STDOUT and nowhere else (never logged, never written to a file) —
the fingerprint is the only identity that ever enters a policy file or an
audit trail.

Usage:
    python scripts/mint_key.py                       # full access (["*"])
    python scripts/mint_key.py --label "ci-runner"   # free-text label in the header comment
    python scripts/mint_key.py --assign "workorder/*,reports/*"

Run from anywhere: the script locates the repo's ``src/`` relative to its own
path and bootstraps ``sys.path``, so the fingerprint always comes from the
checked-out source tree — not from a possibly-stale installed copy.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path


def _bootstrap_src() -> Path:
    """Locate the repo's ``src/`` so the fingerprint comes from audit.py itself.

    Walks up from this script (…/SQLhandler/scripts/mint_key.py) looking for
    ``src/sqlhandler/mcp_fleet_common/audit.py``. Deterministic and offline —
    the script lives inside the repo it bootstraps. Exit with a clear message
    rather than computing the fingerprint locally: a locally-reimplemented
    formula could silently drift from the server's, and a fingerprint that
    doesn't match the audit layer's is worse than no key.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "src" / "sqlhandler" / "mcp_fleet_common" / "audit.py"
        if candidate.is_file():
            return base / "src"
    print(
        "mint_key: could not locate src/sqlhandler/mcp_fleet_common/audit.py "
        f"above {here.parent} — run the script from inside the SQLhandler repo "
        "(the fingerprint MUST be computed by the server's own audit module).",
        file=sys.stderr,
    )
    raise SystemExit(1)


sys.path.insert(0, str(_bootstrap_src()))

from sqlhandler.mcp_fleet_common.audit import key_fingerprint  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mint_key.py",
        description="Mint one SQLhandler /mcp API key + its policy assignments snippet.",
        epilog=(
            "Output goes to stdout only (the raw key is never logged or written "
            "anywhere). Append the key to the api-keys Secret's comma-separated "
            "value and merge the assignments entry into the policy ConfigMap's "
            "datasets.assignments — both hot-reload, no pod restart."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        metavar="TEXT",
        help="free-text label for the key's owner/purpose (appears in the header comment only)",
    )
    parser.add_argument(
        "--assign",
        default="",
        metavar="GLOBS",
        help=(
            'comma-separated dataset globs granted to this key, e.g. '
            '"workorder/*,reports/*"; omitted = ["*"] (full access)'
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    key = secrets.token_urlsafe(32)
    fp = key_fingerprint(key)

    globs: list[str] = []
    if args.assign.strip():
        for raw in args.assign.split(","):
            glob = raw.strip()
            if glob:
                globs.append(glob)
    if not globs:
        globs = ["*"]

    if args.label.strip():
        print(f"# key label: {args.label.strip()}")
    print(f"key         : {key}")
    print(f"fingerprint : {fp}")
    print()
    print("# 1) /mcp API keys — APPEND to the existing comma-separated Secret value")
    print("#    (rotation: append the new key, move clients over, drop the old;")
    print("#     the env is re-read per request — no restart):")
    print(f'SQLHANDLER_API_KEYS="{key},<existing-keys>"')
    print()
    print("# 2) Policy assignments — merge this entry into the policy file's")
    print("#    datasets.assignments object (ConfigMap edit → mtime hot reload):")
    print(json.dumps({fp: globs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
