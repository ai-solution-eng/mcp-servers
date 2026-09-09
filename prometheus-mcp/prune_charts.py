#!/usr/bin/env python3
"""Prune packaged Helm charts in a repo root, keeping only the newest per chart.

Helm's ``helm package`` writes ``<chart-name>-<version>.tgz`` (or ``.tar.gz``)
files, e.g.::

    rag-mcp-server-1.9.0.tar.gz
    rag-mcp-scale-1.9.0-scale.tar.gz
    rag-mcp-scale-g2-1.9.0-g2.tar.gz
    model-downloader-0.4.1.tar.gz, model-downloader-0.4.3.tar.gz

After bumping a chart you end up with several archives for the same chart.
This script keeps only the highest-version archive per chart name and deletes
the rest.  It only touches archives in the repo root (it does not descend into
``helm/charts/`` where dependency archives live).

Usage::
    ./prune_charts.py [<target-repo-root>] [--dry-run]

``<target-repo-root>`` defaults to this script's directory, so it can be
hardlinked into a repo root and run as ``./prune_charts.py``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_ARCHIVE_RE = re.compile(r"^(.+)-(\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?)\.(?:tgz|tar\.gz)$")

_EXCLUDED_DIRS = {"helm", "charts", "vendor", ".mypy_cache", ".ruff_cache", "__pycache__"}


def _version_key(version: str) -> tuple[int, ...]:
    parts = version.split("-")
    nums = tuple(int(x) for x in parts[0].split("."))
    # Archives with a pre-release/suffix (rc1, -scale, ...) sort BELOW the
    # plain release, so "2.2.1" beats "2.2.1-rc1" at the same version.
    suffix = parts[1:] if len(parts) > 1 else ["zzz"]
    return nums + (1 if len(parts) == 1 else 0, len(suffix), suffix[0])


def _find_archives(root: Path) -> list[Path]:
    archives: list[Path] = []
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".tgz", ".gz") or p.name.endswith(".tar.gz"):
            if _ARCHIVE_RE.match(p.name):
                archives.append(p)
    return archives


def _best_per_chart(archives: list[Path]) -> dict[str, tuple[Path, tuple[int, ...]]]:
    best: dict[str, tuple[Path, tuple[int, ...]]] = {}
    for p in archives:
        m = _ARCHIVE_RE.match(p.name)
        assert m is not None
        name, version = m.group(1), m.group(2)
        key = _version_key(version)
        if name not in best or key > best[name][1]:
            best[name] = (p, key)
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", default=None, help="repo root (default: this script's directory)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="only list what would be deleted")
    args = parser.parse_args()

    root = Path(args.root) if args.root else Path(__file__).resolve().parent
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory", file=sys.stderr)
        return 1

    archives = _find_archives(root)
    if not archives:
        print(f"No packaged charts found under {root}")
        return 0

    best = _best_per_chart(archives)
    to_delete = [p for p in archives if p not in {b[0] for b in best.values()}]

    if args.dry_run:
        print("Would delete:")
        for p in to_delete:
            print(f"  {p.name}")
        for name, (keep, _) in sorted(best.items()):
            print(f"  keep {name}: {keep.name}")
        print(f"\n({len(to_delete)} to delete, {len(best)} kept)")
        return 0

    for p in to_delete:
        os.remove(p)
        print(f"[DEL] {p.name}")
    for name, (keep, _) in sorted(best.items()):
        print(f"[KEEP] {name}: {keep.name}")

    if to_delete:
        print(f"\nDone. Removed {len(to_delete)} old chart archive(s), kept {len(best)}.")
    else:
        print("\nNothing to prune — one archive per chart already.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())