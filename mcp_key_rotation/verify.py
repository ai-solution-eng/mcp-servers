#!/usr/bin/env python3
"""verify.py - prove secrets.fleet.yaml covers every Secret the charts reference.

Renders each in-scope chart with helm and compares the (name, key) pairs its
pods ask for against the generated manifest.  Chart-rendered Secrets are
subtracted first: those are helm-owned, and pre-creating them would collide on
ownership metadata.

    python3 fleet_secrets/verify.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = HERE / "secrets.fleet.yaml"

def charts_from_registry() -> dict[str, tuple[str, str | None]]:
    """namespace -> (chart, site values), straight from registry.yaml.

    Deriving this from the registry is deliberate: a hardcoded copy here went
    stale the moment a namespace was corrected (k8s-mcp -> k8s-mcp-ops) and
    reported a phantom coverage gap.
    """
    registry = yaml.safe_load((HERE / "registry.yaml").read_text(encoding="utf-8")) or {}
    charts: dict[str, tuple[str, str | None]] = {}
    for app in registry.get("apps") or []:
        ns = str(app.get("namespace") or "")
        site = app.get("site_values") or []
        site = site if isinstance(site, str) else (site[0] if site else None)
        if ns and app.get("chart"):
            charts[ns] = (str(app["chart"]), site)
    return charts


CHARTS = charts_from_registry()


def main() -> int:
    if not MANIFEST.exists():
        print(f"no {MANIFEST} - run fleet_secrets.py first")
        return 1
    secrets: dict[tuple[str, str], dict] = {}
    for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind") == "Secret":
            meta = doc["metadata"]
            secrets[(meta["namespace"], meta["name"])] = doc.get("stringData") or {}
    print(f"{MANIFEST.name}: {len(secrets)} Secrets, {sum(len(v) for v in secrets.values())} keys")

    ok = True
    for ns, (chart, site) in CHARTS.items():
        cmd = ["helm", "template", "rel", chart, "-n", ns] + (["-f", site] if site else [])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  !! {ns}: render failed - {proc.stderr.strip().splitlines()[-1]}")
            ok = False
            continue
        rendered = proc.stdout
        managed = set(re.findall(r"kind: Secret\s*\nmetadata:\s*\n\s*name: (\S+)", rendered))
        refs = set()
        for match in re.finditer(r"secretKeyRef:", rendered):
            block = rendered[match.end() : match.end() + 200]
            name = re.search(r"name:\s*(\S+)", block)
            key = re.search(r"key:\s*(\S+)", block)
            if name and key and name.group(1) not in managed:
                refs.add((name.group(1), key.group(1)))
        have = {(n, k) for (nsx, n), keys in secrets.items() if nsx == ns for k in keys}
        missing = sorted(r for r in refs if r not in have)
        extra = sorted(r for r in have if r not in refs)
        print(f"  {'OK ' if not missing else 'GAP'} {ns:<16} refs={len(refs):<3} missing={missing or '-'} extra={extra or '-'}")
        ok = ok and not missing
    print("RESULT:", "every chart reference is covered" if ok else "GAPS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
