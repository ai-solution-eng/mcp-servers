"""Tests for the nvlink-topology detection script (no GPUs required).

The parser runs against recorded ``nvidia-smi topo -m`` fixtures covering
the island shapes that matter (two 4-way islands, one NVSwitch island,
pair bridges, no NVLink), and the push path is exercised end-to-end with a
fake nvidia-smi binary against a local HTTP receiver.
"""

import http.server
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

SCRIPT = Path(__file__).resolve().parent.parent / "helm" / "files" / "detect_nvlink.py"
_spec = importlib.util.spec_from_file_location("detect_nvlink", SCRIPT)
detect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect)

# Realistic `nvidia-smi topo -m` for an 8-GPU H200 NVL host: two 4-way
# islands (0-3, 4-7), SYS/PCIe between islands, legend tail to parse past.
TOPO_H200 = (
    "\tGPU0\tGPU1\tGPU2\tGPU3\tGPU4\tGPU5\tGPU6\tGPU7\tCPU Affinity\tNUMA Affinity\n"
    "GPU0\t X \tNV18\tNV18\tNV18\tSYS\tSYS\tSYS\tSYS\t0-55\t0\n"
    "GPU1\tNV18\t X \tNV18\tNV18\tSYS\tSYS\tSYS\tSYS\t0-55\t0\n"
    "GPU2\tNV18\tNV18\t X \tNV18\tSYS\tSYS\tSYS\tSYS\t0-55\t0\n"
    "GPU3\tNV18\tNV18\tNV18\t X \tSYS\tSYS\tSYS\tSYS\t0-55\t0\n"
    "GPU4\tSYS\tSYS\tSYS\tSYS\t X \tNV18\tNV18\tNV18\t56-127\t1\n"
    "GPU5\tSYS\tSYS\tSYS\tSYS\tNV18\t X \tNV18\tNV18\t56-127\t1\n"
    "GPU6\tSYS\tSYS\tSYS\tSYS\tNV18\tNV18\t X \tNV18\t56-127\t1\n"
    "GPU7\tSYS\tSYS\tSYS\tSYS\tNV18\tNV18\tNV18\t X \t56-127\t1\n"
    "\n"
    "Legend:\n"
    "  X    = Self\n"
    "  SYS  = Connection traversing PCIe as well as the SMP interconnect\n"
)


def _topo(edges: set[tuple[int, int]], n: int = 8) -> str:
    """Synthesize a topo matrix from an undirected NVLink edge set."""
    header = "\t" + "\t".join(f"GPU{j}" for j in range(n)) + "\tCPU Affinity\tNUMA Affinity"
    lines = [header]
    for i in range(n):
        cells = [" X " if i == j else ("NV18" if (min(i, j), max(i, j)) in edges else "SYS") for j in range(n)]
        lines.append(f"GPU{i}\t" + "\t".join(cells) + "\t0-55\t0")
    return "\n".join(lines) + "\n\nLegend:\n  X = Self\n"


def test_parse_h200_two_four_way_islands():
    dom, peers = detect.parse_topo(TOPO_H200)
    assert dom == {0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 1, 7: 1}
    assert peers[0] == [1, 2, 3] and peers[7] == [4, 5, 6]


def test_parse_nvswitch_single_island():
    edges = {(i, j) for i in range(8) for j in range(i + 1, 8)}
    dom, peers = detect.parse_topo(_topo(edges))
    assert set(dom.values()) == {0}
    assert peers[3] == [0, 1, 2, 4, 5, 6, 7]


def test_parse_pair_bridges_four_islands():
    edges = {(0, 1), (2, 3), (4, 5), (6, 7)}
    dom, peers = detect.parse_topo(_topo(edges))
    assert dom == {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 6: 3, 7: 3}
    assert peers[0] == [1] and peers[6] == [7]


def test_parse_no_nvlink_is_empty():
    dom, peers = detect.parse_topo(_topo(set()))
    assert dom == {} and peers == {}


def test_parse_strips_ansi_colorized_header():
    """LIVE-CAUGHT BUG: the driver's nvidia-smi colorizes its output even
    when piped — the header arrives underlined (ESC[4m...ESC[0m), the
    escape glues to the first token, the header loses GPU0, every row
    misaligns one column, and phantom cross-island edges merged two real
    4-GPU islands into a bogus 8-GPU one on G2. The real captured shape:
    """
    ansi_topo = (
        "\t\x1b[4mGPU0\tGPU1\tGPU2\tGPU3\tGPU4\tGPU5\tGPU6\tGPU7\tNIC0\tNIC1\tNIC2\tNIC3\tNIC4\tNIC5\tNIC6\tNIC7"
        "\tCPU Affinity\tNUMA Affinity\tGPU NUMA ID\x1b[0m\n"
        "GPU0\t X \tNV6\tNV6\tNV6\tSYS\tSYS\tSYS\tSYS\tNODE\tNODE\tSYS\tSYS\tSYS\tSYS\tSYS\tSYS\t0-85,172-257\t0\tN/A\n"
        "GPU1\tNV6\t X \tNV6\tNV6\tSYS\tSYS\tSYS\tSYS\tNODE\tNODE\tSYS\tSYS\tSYS\tSYS\tSYS\tSYS\t0-85,172-257\t0\tN/A\n"
        "GPU2\tNV6\tNV6\t X \tNV6\tSYS\tSYS\tSYS\tSYS\tNODE\tNODE\tSYS\tSYS\tSYS\tSYS\tSYS\tSYS\t0-85,172-257\t0\tN/A\n"
        "GPU3\tNV6\tNV6\tNV6\t X \tSYS\tSYS\tSYS\tSYS\tNODE\tNODE\tSYS\tSYS\tSYS\tSYS\tSYS\tSYS\t0-85,172-257\t0\tN/A\n"
        "GPU4\tSYS\tSYS\tSYS\tSYS\t X \tNV6\tNV6\tNV6\tSYS\tSYS\tNODE\tNODE\tNODE\tNODE\tNODE\tNODE\t86-171,258-343\t1\tN/A\n"
        "GPU5\tSYS\tSYS\tSYS\tSYS\tNV6\t X \tNV6\tNV6\tSYS\tSYS\tNODE\tNODE\tNODE\tNODE\tNODE\tNODE\t86-171,258-343\t1\tN/A\n"
        "GPU6\tSYS\tSYS\tSYS\tSYS\tNV6\tNV6\t X \tNV6\tSYS\tSYS\tNODE\tNODE\tNODE\tNODE\tNODE\tNODE\t86-171,258-343\t1\tN/A\n"
        "GPU7\tSYS\tSYS\tSYS\tSYS\tNV6\tNV6\tNV6\t X \tSYS\tSYS\tNODE\tNODE\tNODE\tNODE\tNODE\tNODE\t86-171,258-343\t1\tN/A\n"
    )
    dom, peers = detect.parse_topo(ansi_topo)
    # the two REAL 4-GPU islands — never a merged 8
    assert dom == {0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 1, 7: 1}
    assert peers[0] == [1, 2, 3] and peers[4] == [5, 6, 7]


def test_build_body_format_and_empty_clear():
    dom = {0: 0, 1: 0, 4: 1}
    peers = {0: [1], 1: [0], 4: []}
    body = detect.build_body("node-a", dom, peers)
    assert 'nvidia_gpu_nvlink_domain{gpu="0",domain="0",peers="1",hostname="node-a"} 1' in body
    assert 'nvidia_gpu_nvlink_domain{gpu="4",domain="1",peers="",hostname="node-a"} 1' in body
    assert body.endswith("\n")
    assert detect.build_body("node-a", {}, {}) == ""  # empty push clears stale grouping


class _Capture(http.server.BaseHTTPRequestHandler):
    captured: ClassVar[list[tuple[str, str]]] = []

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        _Capture.captured.append((self.path, body))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_end_to_end_with_fake_nvidia_smi(tmp_path):
    fake = tmp_path / "nvidia-smi"
    fake.write_text("#!/bin/sh\ncat <<'TOPO'\n" + TOPO_H200 + "TOPO\n")
    fake.chmod(0o755)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(
            os.environ,
            NVIDIA_SMI=str(fake),
            PUSHGATEWAY_URL=f"http://127.0.0.1:{srv.server_address[1]}",
            NODE_NAME="gpu-node-7",
        )
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        path, body = _Capture.captured[-1]
        assert path == "/metrics/job/nvlink-topology/instance/gpu-node-7"
        assert 'nvidia_gpu_nvlink_domain{gpu="0",domain="0",peers="1,2,3",hostname="gpu-node-7"} 1' in body
        assert 'nvidia_gpu_nvlink_domain{gpu="7",domain="1",peers="4,5,6",hostname="gpu-node-7"} 1' in body
        assert "2 NVLink island(s)" in r.stdout
    finally:
        srv.shutdown()


def test_topo_file_mode_end_to_end(tmp_path):
    """The deployed path: initContainer writes topo.txt, detector reads it."""
    topo = tmp_path / "topo.txt"
    topo.write_text(TOPO_H200)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(
            os.environ,
            TOPO_FILE=str(topo),
            PUSHGATEWAY_URL=f"http://127.0.0.1:{srv.server_address[1]}",
            NODE_NAME="file-node",
        )
        r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        path, body = _Capture.captured[-1]
        assert path == "/metrics/job/nvlink-topology/instance/file-node"
        assert 'nvidia_gpu_nvlink_domain{gpu="4",domain="1",peers="5,6,7",hostname="file-node"} 1' in body
        assert "2 NVLink island(s)" in r.stdout
    finally:
        srv.shutdown()


def test_topo_file_missing_is_a_failure(tmp_path):
    env = dict(
        os.environ,
        TOPO_FILE=str(tmp_path / "absent.txt"),
        PUSHGATEWAY_URL="http://127.0.0.1:1",
        NODE_NAME="n",
    )
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "reading" in r.stderr


def test_missing_driver_is_quiet_success(tmp_path):
    env = dict(
        os.environ,
        NVIDIA_SMI=str(tmp_path / "no-such-binary"),
        PUSHGATEWAY_URL="http://127.0.0.1:1",
        NODE_NAME="cpu-node",
    )
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "no NVIDIA driver" in r.stdout


def test_push_failure_is_a_loud_failure(tmp_path):
    fake = tmp_path / "nvidia-smi"
    fake.write_text("#!/bin/sh\ncat <<'TOPO'\n" + TOPO_H200 + "TOPO\n")
    fake.chmod(0o755)
    env = dict(
        os.environ,
        NVIDIA_SMI=str(fake),
        PUSHGATEWAY_URL="http://127.0.0.1:1",  # nothing listens there
        NODE_NAME="gpu-node-7",
    )
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "push" in r.stderr.lower()


def test_missing_pushgateway_url_is_misconfiguration():
    env = dict(os.environ, NVIDIA_SMI="nvidia-smi", PUSHGATEWAY_URL="", NODE_NAME="n")
    env.pop("PUSHGATEWAY_URL", None)
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
