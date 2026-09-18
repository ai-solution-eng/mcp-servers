#!/usr/bin/env python3
"""Detect NVLink islands (domains) on this GPU node and publish them.

Runs ``nvidia-smi topo -m`` once, computes the connected components of the
NVLink graph (union-find over the NV# matrix cells), and pushes an info
metric to the Prometheus pushgateway:

    nvidia_gpu_nvlink_domain{gpu="0",domain="0",peers="1,2,3",hostname="node"} 1

Deployed by the prometheus-mcp chart as the ``nvlink-topology`` DaemonSet,
as a TWO-container pod: an initContainer (the cluster's NVIDIA driver
image, privileged) writes ``nvidia-smi topo -m`` to a shared emptyDir, and
this script (python:3.12-slim — the driver image ships no python3) reads
it via the ``TOPO_FILE`` env and pushes. Standalone use without TOPO_FILE
runs ``nvidia-smi`` directly instead. Detection happens once per pod
lifetime (= node boot; the pod is recreated on reboot, which re-detects) —
NVLink topology is hardware-fixed and cannot change without a reboot or
hardware change.

The prometheus-mcp GPU tab prefers this metric over its built-in default
grouping; an explicit PROM_UI_GPU_NVLINK_DOMAINS override still wins as the
operator escape hatch. The consumer matches hosts on the first dot-label
(k8s node name vs DCGM's fqdn Hostname), so short or fqdn node names both
join correctly.

Exit codes: 0 = detected (or honestly nothing to detect: no NVIDIA driver
on this node); 1 = detection or push failed (the supervisor retries in 60s
and the pod logs the reason); 2 = misconfiguration (missing
PUSHGATEWAY_URL).
"""

import os
import re
import socket
import subprocess
import sys
import urllib.request
from urllib.error import URLError

NVIDIA_SMI = os.environ.get("NVIDIA_SMI", "nvidia-smi")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "").rstrip("/")
NODE_NAME = os.environ.get("NODE_NAME") or socket.gethostname()
JOB = "nvlink-topology"
_METRIC = "nvidia_gpu_nvlink_domain"
_SMI_TIMEOUT = 60
_PUSH_TIMEOUT = 15

_GPU_ROW = re.compile(r"^GPU(\d+)$")
_NV_CELL = re.compile(r"^NV(\d+)$")
# The driver's nvidia-smi colorizes its output (ANSI styles) even when
# piped — on G2 the header row arrives underlined, gluing ESC[4m to the
# first "GPU0" token. An unstripped header then loses GPU0, every data row
# misaligns by one column against it, and phantom cross-island NV edges
# merge two 4-GPU islands into one bogus 8-GPU island. Strip ALL CSI
# sequences before parsing.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_topo(text: str) -> tuple[dict[int, int], dict[int, list[int]]]:
    """``nvidia-smi topo -m`` output -> ({gpu: domain}, {gpu: [peers]}).

    The first row of GPU<i> tokens is the matrix header (it also carries
    CPU/NUMA columns, which are ignored); every later GPU<i> row is a data
    row whose cells describe the link to the header-column GPU. A cell
    matching ``NV<n>`` is an NVLink edge; PIX/PHB/NODE/SYS/C2C/X cells are
    not. Domains are the connected components, numbered by their smallest
    GPU index (so {0,1,2,3} -> 0, {4,5,6,7} -> 1, matching the UI default).
    Only GPUs with at least one NVLink edge are reported — PCIe-only GPUs
    belong to no island.
    """
    header: list[int] = []
    edges: list[tuple[int, int]] = []
    for raw_line in text.splitlines():
        line = _ANSI_CSI.sub("", raw_line)
        cells = line.strip().split()
        if not cells:
            continue
        if not header:
            cols = [int(m.group(1)) for c in cells if (m := _GPU_ROW.match(c))]
            if cols:
                header = cols
            continue
        row = _GPU_ROW.match(cells[0])
        if not row:
            continue  # legend / tail
        for offset, cell in enumerate(cells[1 : 1 + len(header)]):
            nv = _NV_CELL.match(cell)
            if nv:
                edges.append((int(row.group(1)), header[offset]))
    return _domains_from_edges(edges)


def _domains_from_edges(edges: list[tuple[int, int]]) -> tuple[dict[int, int], dict[int, list[int]]]:
    """NVLink edge list -> ({gpu: domain}, {gpu: [peers]}) via union-find."""
    parent: dict[int, int] = {g: g for edge in edges for g in edge}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)

    components: dict[int, list[int]] = {}
    for g in sorted(parent):
        components.setdefault(find(g), []).append(g)

    dom: dict[int, int] = {}
    peers: dict[int, list[int]] = {}
    for di, root in enumerate(sorted(components, key=lambda r: min(components[r]))):
        members = sorted(components[root])
        for g in members:
            dom[g] = di
            peers[g] = [m for m in members if m != g]
    return dom, peers


def build_body(host: str, dom: dict[int, int], peers: dict[int, list[int]]) -> str:
    """Exposition-format push body (empty string clears a previous push)."""
    lines = []
    for g in sorted(dom):
        labels = f'gpu="{g}",domain="{dom[g]}",peers="{",".join(str(p) for p in peers[g])}",hostname="{host}"'
        lines.append(f"{_METRIC}{{{labels}}} 1")
    return ("\n".join(lines) + "\n") if lines else ""


def push(url: str, body: str) -> None:
    req = urllib.request.Request(url, data=body.encode(), method="PUT", headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=_PUSH_TIMEOUT) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise URLError(f"pushgateway returned HTTP {resp.status}")


def load_topo() -> tuple[str | None, int]:
    """Fetch the topo matrix text. Returns (text, exit_code_if_failed).

    Source priority: ``TOPO_FILE`` (written by the pod's initContainer —
    the driver image has no python3, so the detector reads a file instead
    of exec'ing nvidia-smi), else run ``NVIDIA_SMI topo -m`` directly
    (standalone use).
    """
    path = os.environ.get("TOPO_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read(), 0
        except OSError as exc:
            print(f"reading {path} failed: {exc}", file=sys.stderr)
            return None, 1
    try:
        proc = subprocess.run(
            [NVIDIA_SMI, "topo", "-m"],
            capture_output=True,
            text=True,
            timeout=_SMI_TIMEOUT,
        )
        proc.check_returncode()
    except FileNotFoundError:
        print(f"{NVIDIA_SMI} not present — no NVIDIA driver on this node, nothing to detect")
        return None, 0  # quiet on non-GPU nodes (no nodeSelector needed)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"{NVIDIA_SMI} topo failed: {exc}", file=sys.stderr)
        return None, 1
    return proc.stdout, 0


def main() -> int:
    if not PUSHGATEWAY_URL:
        print("PUSHGATEWAY_URL is required", file=sys.stderr)
        return 2
    topo_text, err = load_topo()
    if err:
        return err
    if topo_text is None:
        # "No NVIDIA driver on this node" — quiet success, nothing to push.
        return 0

    dom, peers = parse_topo(topo_text)
    url = f"{PUSHGATEWAY_URL}/metrics/job/{JOB}/instance/{NODE_NAME}"
    try:
        push(url, build_body(NODE_NAME, dom, peers))
    except (URLError, OSError) as exc:
        print(f"push to {url} failed: {exc}", file=sys.stderr)
        return 1

    if dom:
        summary = "; ".join(
            f"domain {d}: GPUs {','.join(str(g) for g in sorted(dom) if dom[g] == d)}"
            for d in sorted(set(dom.values()))
        )
        print(f"pushed {len(dom)} GPUs in {len(set(dom.values()))} NVLink island(s) for {NODE_NAME}: {summary}")
        # Raw parsed adjacency — the evidence behind the grouping, so the
        # topo matrix's claim is always auditable from `kubectl logs`.
        for g in sorted(dom):
            print(f"  GPU {g}: NV-peers {','.join(str(p) for p in peers[g]) or '(none)'}")
    else:
        print(f"no NVLink islands on {NODE_NAME} (cleared any previous grouping)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
