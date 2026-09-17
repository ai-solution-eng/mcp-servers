"""Standalone checks for the EzApp deploy MCP server's validation layers.

Run with:  python3 test_ezapp_deploy.py

The `mcp` SDK and `yaml` are stubbed/required as available: `yaml` is a
hard dependency of server.py (install PyYAML to run these checks), while
the MCP SDK is stubbed when missing so the module imports without a
virtualenv. No cluster is needed — everything here exercises pure
validation logic and in-memory tarballs.
"""

import asyncio
import base64
import binascii
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import types

import yaml


def _install_mcp_stubs():
    """Minimal stubs for the mcp import surface server.py needs."""
    if "mcp" in sys.modules:
        return
    mcp_pkg = types.ModuleType("mcp")
    server_pkg = types.ModuleType("mcp.server")

    class MCPServer:
        def __init__(self, *args, **kwargs):
            self.tools = []

            def tool():
                def decorator(fn):
                    self.tools.append(fn)
                    return fn
                return decorator

            self.tool = tool

    def streamable_http_app(self, **kwargs):
        raise RuntimeError("network transport not under test")

    MCPServer.streamable_http_app = streamable_http_app
    mcpserver_mod = types.ModuleType("mcp.server.mcpserver")
    mcpserver_mod.MCPServer = MCPServer

    class CacheHint:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    caching_mod = types.ModuleType("mcp.server.caching")
    caching_mod.CacheHint = CacheHint

    class TransportSecuritySettings:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    transport_mod = types.ModuleType("mcp.server.transport_security")
    transport_mod.TransportSecuritySettings = TransportSecuritySettings

    mcp_pkg.server = server_pkg
    server_pkg.mcpserver = mcpserver_mod
    server_pkg.caching = caching_mod
    server_pkg.transport_security = transport_mod
    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.server"] = server_pkg
    sys.modules["mcp.server.mcpserver"] = mcpserver_mod
    sys.modules["mcp.server.caching"] = caching_mod
    sys.modules["mcp.server.transport_security"] = transport_mod


_install_mcp_stubs()

import server  # noqa: E402  (stubs must be in place first)


# ─── Tiny assert harness ─────────────────────────────────────────────────

_PASS = 0
_FAIL = 0


def check(label, condition, detail=""):
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  ok  {label}")
    else:
        _FAIL += 1
        print(f" FAIL {label}  {detail}")


def check_raises(label, message_part, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        check(label, message_part.lower() in str(e).lower(),
              f"expected error containing {message_part!r}, got {e!r}")
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"unexpected {type(e).__name__}: {e}")
        return
    check(label, False, f"expected ValueError containing {message_part!r}, no error raised")


def set_env(**kwargs):
    for key, value in kwargs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ─── Test data ───────────────────────────────────────────────────────────

VALID_EZAPPCONFIG = """\
apiVersion: ezconfig.hpe.ezaf.com/v1alpha1
kind: EzAppConfig
metadata:
  name: ezappconfig-test-app
  annotations:
    hpe-ezua/cleanup-chart-on-delete: "true"
  labels:
    hpe-ezua/imported-app: "true"
spec:
  name: test-app
  install: true
  releaseName: test-app
  chartVersion: 0.2.6
  description: Test app description
  label: Test App
  category: dataScience
  options:
    namespace: test-app-ns
    create-namespace: "true"
    wait: "true"
    timeout: 15m
  values: ""
"""


def make_chart_tgz(chart_name="test-app", version="0.2.6", extra_members=None,
                   chart_yaml=None):
    """Build a helm-package-style tarball in memory (name/Chart.yaml)."""
    if chart_yaml is None:
        chart_yaml = f"apiVersion: v2\nname: {chart_name}\nversion: {version}\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, payload):
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add(f"{chart_name}/Chart.yaml", chart_yaml)
        add(f"{chart_name}/values.yaml", "replicaCount: 1\n")
        add(f"{chart_name}/templates/deployment.yaml", "apiVersion: v1\nkind: X\n")
        for member in extra_members or []:
            add(member[0], member[1])
    return buf.getvalue()


# ─── 1. EzAppConfig manifest validation ──────────────────────────────────

print("1. EzAppConfig manifest validation")

set_env(EZAPP_MCP_EZAPPCONFIG_KIND=None,
        EZAPP_MCP_EZAPPCONFIG_API_VERSIONS=None,
        EZAPP_MCP_ALLOWED_TARGET_NAMESPACES=None,
        EZAPP_MCP_BLOCKED_TARGET_NAMESPACES=None)

doc = server._validate_ezappconfig(VALID_EZAPPCONFIG)
check("valid tutorial-shaped manifest passes", doc["spec"]["chartVersion"] == "0.2.6")

check_raises("wrong kind is rejected", "only kind: ezappconfig",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("kind: EzAppConfig", "kind: Secret"))
check_raises("multi-document yaml is rejected", "exactly one yaml document",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG + "---\n" + VALID_EZAPPCONFIG)
check_raises("metadata.namespace is rejected (cluster-scoped)",
             "cluster-scoped",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace(
                 "  name: ezappconfig-test-app",
                 "  name: ezappconfig-test-app\n  namespace: foo"))
check_raises("bad metadata.name is rejected", "metadata.name",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace(
                 "name: ezappconfig-test-app", "name: Bad_Name!"))
check_raises("missing spec.chartVersion is rejected", "spec.chartversion",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("  chartVersion: 0.2.6\n", ""))
check_raises("missing spec.name is rejected", "spec.name",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("  name: test-app\n", ""))
check_raises("invalid yaml is rejected", "not valid yaml",
             server._validate_ezappconfig, "kind: [unclosed")
check_raises("empty manifest is rejected", "empty",
             server._validate_ezappconfig, "   ")
check_raises("kube-system target is always denied", "never a valid deploy target",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("namespace: test-app-ns", "namespace: kube-system"))

set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS="ezconfig.hpe.ezaf.com/v1alpha2,ezconfig.hpe.ezaf.com/v1beta1")
check_raises("apiVersion outside allowlist is rejected", "allowlist",
             server._validate_ezappconfig, VALID_EZAPPCONFIG)
check("apiVersion inside allowlist passes",
      server._validate_ezappconfig(
          VALID_EZAPPCONFIG.replace("v1alpha1", "v1alpha2")
      ) is not None)
set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS="*")
check("apiVersion allowlist '*' accepts any",
      server._validate_ezappconfig(VALID_EZAPPCONFIG) is not None)
set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS=None)

check("no options namespace passes policy",
      server._validate_ezappconfig(
          VALID_EZAPPCONFIG.replace("    namespace: test-app-ns\n", "")
      ) is not None)

set_env(EZAPP_MCP_ALLOWED_TARGET_NAMESPACES="team-*,test-app-ns")
check("allowed-list match passes",
      server._validate_ezappconfig(VALID_EZAPPCONFIG) is not None)
check_raises("allowed-list miss is rejected", "not covered",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("test-app-ns", "other-ns"))
set_env(EZAPP_MCP_ALLOWED_TARGET_NAMESPACES=None,
        EZAPP_MCP_BLOCKED_TARGET_NAMESPACES="sneaky-*")
check("non-blocked namespace passes with blacklist only",
      server._validate_ezappconfig(VALID_EZAPPCONFIG) is not None)
check_raises("blacklist match is rejected", "denied by the target-namespace policy",
             server._validate_ezappconfig,
             VALID_EZAPPCONFIG.replace("test-app-ns", "sneaky-ns"))
set_env(EZAPP_MCP_BLOCKED_TARGET_NAMESPACES=None)

set_env(EZAPP_MCP_EZAPPCONFIG_KIND="MyAppConfig")
check_raises("custom kind env is honored", "only kind: myappconfig",
             server._validate_ezappconfig, VALID_EZAPPCONFIG)
check("custom kind passes when the manifest matches",
      server._validate_ezappconfig(
          VALID_EZAPPCONFIG.replace("kind: EzAppConfig", "kind: MyAppConfig")
      ) is not None)
set_env(EZAPP_MCP_EZAPPCONFIG_KIND=None)


# ─── 2. Chart tarball sniffing ───────────────────────────────────────────

print("2. Chart tarball validation")

name, version = server._inspect_chart_tarball(make_chart_tgz())
check("valid tarball yields name/version",
      (name, version) == ("test-app", "0.2.6"), f"got {(name, version)}")

name, version = server._inspect_chart_tarball(
    make_chart_tgz(chart_yaml="apiVersion: v2\nname: my.chart_name\nversion: 1.0.0\n")
)
check("chart name regex accepts dots/underscores", name == "my.chart_name")

def _tgz_without_chart_yaml():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"replicaCount: 1\n"
        info = tarfile.TarInfo(name="mychart/values.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


check_raises("non-gzip payload is rejected", "not gzip data",
             server._inspect_chart_tarball, b"PK\x03\x04-not-a-tgz")
check_raises("gzip without tar structure is rejected", "gzipped tar",
             server._inspect_chart_tarball, gzip.compress(b"plain text"))
check_raises("tarball without Chart.yaml is rejected", "chart.yaml",
             server._inspect_chart_tarball, _tgz_without_chart_yaml())
check_raises("Chart.yaml without version is rejected", "version",
             server._inspect_chart_tarball,
             make_chart_tgz(chart_yaml="apiVersion: v2\nname: x\n"))
check_raises("unparseable Chart.yaml is rejected", "yaml",
             server._inspect_chart_tarball,
             make_chart_tgz(chart_yaml="name: [unclosed"))

# Decompressed-size (zip-bomb) cap: lower the module constant instead of
# building a multi-GB stream — the sum-of-member-sizes check reads headers
# of a real (small) tar either way.
_saved_tar_cap = server.MAX_TAR_UNCOMPRESSED_BYTES
server.MAX_TAR_UNCOMPRESSED_BYTES = 1024
try:
    check_raises("decompressed tar total is capped", "beyond the safety cap",
                 server._inspect_chart_tarball,
                 make_chart_tgz(extra_members=[("big.bin", "A" * 4096)]))
finally:
    server.MAX_TAR_UNCOMPRESSED_BYTES = _saved_tar_cap

set_env(EZAPP_MCP_MAX_CHART_MB="1")
small = make_chart_tgz()
check("tarball under a low cap passes",
      server._inspect_chart_tarball(small)[0] == "test-app")
big = small + b"\x00" * (1024 * 1024 + 10)
check_raises("tarball above the cap is rejected", "cap",
             server._inspect_chart_tarball, big)
set_env(EZAPP_MCP_MAX_CHART_MB=None)


# ─── 3. Base64 decode path + curl argv building ──────────────────────────

print("3. Upload plumbing (base64 + argv)")

b64 = base64.b64encode(make_chart_tgz()).decode()
try:
    decoded = base64.b64decode("".join(b64.split()), validate=True)
    check("b64 with whitespace stripped decodes", len(decoded) > 0)
except (binascii.Error, ValueError) as e:
    check("b64 with whitespace stripped decodes", False, str(e))

try:
    base64.b64decode("not-b64!!", validate=True)
    check("invalid base64 is rejected by validate=True", False, "no error")
except (binascii.Error, ValueError):
    check("invalid base64 is rejected by validate=True", True)

argv = ["curl", "-sS", "-m", str(server.CURL_TIMEOUT_SECONDS),
        *server._chartmuseum_curl_auth_argv(),
        "--data-binary", "@/tmp/x.tgz", "-w", "%{http_code}",
        "http://chartmuseum.ez-chartmuseum-ns:8080/api/charts?force=true"]
check("upload argv shape (raw --data-binary, -w code, force query)",
      argv[0] == "curl" and "--data-binary" in argv
      and argv[-1].endswith("/api/charts?force=true"), str(argv))

set_env(CHARTMUSEUM_USERNAME="alice", CHARTMUSEUM_PASSWORD="s3cret:with:colons")
auth = server._chartmuseum_curl_auth_argv()
check("basic-auth argv uses a single -u user:pass token",
      auth == ["-u", "alice:s3cret:with:colons"], str(auth))
set_env(CHARTMUSEUM_USERNAME=None, CHARTMUSEUM_PASSWORD=None)

check("chartmuseum report: 404 delete is a benign no-op",
      "nothing to delete" in server._chartmuseum_report(404, "", "delete", "a/1.0.0"))
check("chartmuseum report: 500 is an error",
      "Error:" in server._chartmuseum_report(500, "", "delete", "a/1.0.0"))


# ─── 4. Derived config ───────────────────────────────────────────────────

print("4. Derived configuration")

set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS=None,
        EZAPP_MCP_EZAPPCONFIG_PLURAL=None)
check("default kind is EzAppConfig", server._ezappconfig_kind() == "EzAppConfig")
check("default apiVersion is the ezaf one",
      server._ezappconfig_api_versions() == ("ezconfig.hpe.ezaf.com/v1alpha1",))
check("default group derives from apiVersion",
      server._ezappconfig_group() == "ezconfig.hpe.ezaf.com")
check("default plural is ezappconfigs", server._ezappconfig_plural() == "ezappconfigs")
set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS="custom.io/v3")
check("group derives from a custom apiVersion", server._ezappconfig_group() == "custom.io")
set_env(EZAPP_MCP_EZAPPCONFIG_API_VERSIONS=None)


# ─── 5. Async tool smoke tests (validation-only paths, no subprocess) ────

print("5. Tool-level error paths")

async def _tool_error_paths():
    results = {}
    results["upload_bad_b64"] = await server.upload_chart("!!!not-b64!!!")
    results["upload_empty"] = await server.upload_chart("")
    results["apply_empty"] = await server.apply_ezappconfig("")
    results["apply_bad_kind"] = await server.apply_ezappconfig(
        "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\nspec: {}\n")
    results["get_bad_name"] = await server.get_ezappconfig("Bad Name!")
    results["get_bad_output"] = await server.get_ezappconfig("ok-name", output="wide")
    results["delete_bad_name"] = await server.delete_chart("Bad/Name", "1.0.0")
    results["delete_bad_version"] = await server.delete_chart("ok-name", "../etc")
    return results

set_env(CHARTMUSEUM_URL=None)  # default URL kicks in; tools must still fail on payloads
results = asyncio.run(_tool_error_paths())
check("upload_chart rejects invalid base64", results["upload_bad_b64"].startswith("Error:"),
      results["upload_bad_b64"])
check("upload_chart rejects empty payload", results["upload_empty"].startswith("Error:"),
      results["upload_empty"])
check("apply_ezappconfig rejects empty manifest", results["apply_empty"].startswith("Error:"),
      results["apply_empty"])
check("apply_ezappconfig rejects non-EzAppConfig kind", results["apply_bad_kind"].startswith("Error:"),
      results["apply_bad_kind"])
check("get_ezappconfig rejects invalid names", results["get_bad_name"].startswith("Error:"),
      results["get_bad_name"])
check("get_ezappconfig rejects unsupported output", results["get_bad_output"].startswith("Error:"),
      results["get_bad_output"])
check("delete_chart rejects invalid chart names", results["delete_bad_name"].startswith("Error:"),
      results["delete_bad_name"])
check("delete_chart rejects path-traversal versions", results["delete_bad_version"].startswith("Error:"),
      results["delete_bad_version"])


# ─── 6. Ownership ledger: deletes/overwrites limited to self-deployed ────

print("6. Ownership ledger (force-upload, delete_chart, delete_ezappconfig, apply)")

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402


class FakeLedger:
    """In-memory stand-in with the _Ledger interface."""

    name = "ezapp-deploy-ledger"
    namespace = "default"

    def __init__(self, charts=None, apps=None):
        self.charts = dict(charts or {})
        self.apps = dict(apps or {})
        self.saved = 0

    async def load(self):
        return dict(self.charts), dict(self.apps)

    async def save(self, charts, apps):
        self.charts, self.apps = dict(charts), dict(apps)
        self.saved += 1

    async def record_chart(self, n, v, filename=""):
        c, a = await self.load()
        c[f"{n}/{v}"] = {"uploaded_at": "t", "filename": filename}
        await self.save(c, a)

    async def record_ezappconfig(self, name, chart, cv, ns):
        c, a = await self.load()
        a[name] = {"applied_at": "t", "chart": chart,
                   "chart_version": cv, "target_namespace": ns}
        await self.save(c, a)

    async def forget_chart(self, n, v):
        c, a = await self.load()
        c.pop(f"{n}/{v}", None)
        await self.save(c, a)

    async def forget_ezappconfig(self, name):
        c, a = await self.load()
        a.pop(name, None)
        await self.save(c, a)

    async def has_chart(self, n, v):
        c, _ = await self.load()
        return f"{n}/{v}" in c

    async def has_ezappconfig(self, name):
        _, a = await self.load()
        return name in a


class _StubChartMuseum:
    """Local HTTP stub returning a scripted list of status codes in order."""

    def __init__(self, script):
        state = {"i": 0, "requests": []}

        class Handler(BaseHTTPRequestHandler):
            def _respond(self):
                state["requests"].append((self.command, self.path))
                code = script[min(state["i"], len(script) - 1)]
                state["i"] += 1
                body = b'{"data":{}}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = _respond
            do_DELETE = _respond

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.state = state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ScriptedSubprocess:
    """Replaces server._run_subprocess with scripted (rc, out, err) replies."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self._original = server._run_subprocess

    async def fake(self, argv, timeout):
        self.calls.append(list(argv))
        return self.script.pop(0) if self.script else (1, "", "script exhausted")

    def install(self):
        server._run_subprocess = self.fake
        return self

    def restore(self):
        server._run_subprocess = self._original


B64_CHART = base64.b64encode(make_chart_tgz()).decode()
RESOURCE = "ezappconfigs.ezconfig.hpe.ezaf.com"


async def _ledger_scenarios():
    out = {}

    # 1. force=true on an unmanaged chart → refused before any network call
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url)
    ledger = FakeLedger()
    server._ledger_instance = ledger
    out["force_unmanaged"] = await server.upload_chart(B64_CHART, force=True)
    out["force_unmanaged_requests"] = list(stub.state["requests"])
    stub.stop()

    # 2. force=true on a managed chart → allowed; 201 recorded in ledger
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url)
    ledger = FakeLedger(charts={"test-app/0.2.6": {"uploaded_at": "t"}})
    server._ledger_instance = ledger
    out["force_managed"] = await server.upload_chart(B64_CHART, force=True)
    out["force_managed_ledger"] = dict(ledger.charts)
    stub.stop()

    # 3. plain upload 201 → recorded in ledger
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url)
    ledger = FakeLedger()
    server._ledger_instance = ledger
    out["upload_201"] = await server.upload_chart(B64_CHART, filename="test-app-0.2.6.tgz")
    out["upload_201_ledger"] = dict(ledger.charts)
    stub.stop()

    # 4. 409 responses distinguish managed vs unmanaged
    stub = _StubChartMuseum([409, 409])
    set_env(CHARTMUSEUM_URL=stub.url)
    server._ledger_instance = FakeLedger()          # unmanaged
    out["conflict_unmanaged"] = await server.upload_chart(B64_CHART)
    server._ledger_instance = FakeLedger(charts={"test-app/0.2.6": {}})  # managed
    out["conflict_managed"] = await server.upload_chart(B64_CHART)
    stub.stop()

    # 5. delete_chart: unmanaged refused; managed+200 forgotten; managed+404 forgotten
    stub = _StubChartMuseum([200, 404])
    set_env(CHARTMUSEUM_URL=stub.url)
    ledger = FakeLedger(charts={"test-app/0.2.6": {}})
    server._ledger_instance = ledger
    out["delete_unmanaged"] = await server.delete_chart("other", "1.0.0")
    out["delete_unmanaged_requests"] = list(stub.state["requests"])
    out["delete_ok"] = await server.delete_chart("test-app", "0.2.6")
    out["delete_ok_ledger"] = dict(ledger.charts)
    ledger.charts["test-app/0.2.6"] = {}
    out["delete_gone"] = await server.delete_chart("test-app", "0.2.6")
    out["delete_gone_ledger"] = dict(ledger.charts)
    stub.stop()

    # 6. apply: fresh CR → get NotFound, apply ok, ledger records
    ledger = FakeLedger()
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([
        (1, "", f"Error from server (NotFound): {RESOURCE}.\"ezappconfig-test-app\" not found"),
        (0, "ezappconfig.ezconfig.hpe.ezaf.com/ezappconfig-test-app created", ""),
    ]).install()
    out["apply_fresh"] = await server.apply_ezappconfig(VALID_EZAPPCONFIG)
    out["apply_fresh_ledger"] = dict(ledger.apps)
    out["apply_fresh_calls"] = [c[:2] for c in sub.calls]
    sub.restore()

    # 7. apply: existing UNMANAGED CR → refused (only the get runs)
    ledger = FakeLedger()
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([(0, f"{RESOURCE}/ezappconfig-test-app", "")]).install()
    out["apply_unmanaged"] = await server.apply_ezappconfig(VALID_EZAPPCONFIG)
    out["apply_unmanaged_calls"] = len(sub.calls)
    sub.restore()

    # 8. apply: existing MANAGED CR (upgrade) → allowed, ledger kept
    ledger = FakeLedger(apps={"ezappconfig-test-app": {"chart": "test-app"}})
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([
        (0, f"{RESOURCE}/ezappconfig-test-app", ""),
        (0, f"{RESOURCE}/ezappconfig-test-app configured", ""),
    ]).install()
    out["apply_upgrade"] = await server.apply_ezappconfig(VALID_EZAPPCONFIG)
    out["apply_upgrade_ledger"] = dict(ledger.apps)
    sub.restore()

    # 9. apply: existence check fails hard (no NotFound) → refuse
    server._ledger_instance = FakeLedger()
    sub = ScriptedSubprocess([(1, "", "forbidden")]).install()
    out["apply_check_fail"] = await server.apply_ezappconfig(VALID_EZAPPCONFIG)
    sub.restore()

    # 10. delete_ezappconfig: unmanaged refused without subprocess
    ledger = FakeLedger()
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([]).install()
    out["ezdelete_unmanaged"] = await server.delete_ezappconfig("ezappconfig-test-app")
    out["ezdelete_unmanaged_calls"] = len(sub.calls)
    sub.restore()

    # 12. delete_ezappconfig: managed + NotFound → stale entry removed
    ledger = FakeLedger(apps={"ezappconfig-test-app": {}})
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([
        (1, "", f"Error from server (NotFound): {RESOURCE}.\"ezappconfig-test-app\" not found"),
    ]).install()
    out["ezdelete_gone"] = await server.delete_ezappconfig("ezappconfig-test-app")
    out["ezdelete_gone_ledger"] = dict(ledger.apps)
    sub.restore()

    # 12b. delete accepted (--wait=false) → entry removed in the SAME call,
    #      and a bounded background teardown watch is scheduled
    server._TEARDOWN_POLL_SECONDS = 0.0
    server._TEARDOWN_MAX_POLLS = 2
    ledger = FakeLedger(apps={"ezappconfig-test-app": {}})
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([
        (0, f"{RESOURCE}/ezappconfig-test-app deleted", ""),   # delete accepted
        (1, "", f"Error from server (NotFound): {RESOURCE}.\"ezappconfig-test-app\" not found"),  # watch poll 1: gone
    ]).install()
    out["ezdelete_ok"] = await server.delete_ezappconfig("ezappconfig-test-app")
    watcher = server._teardown_watchers.get("ezappconfig-test-app")
    if watcher is not None:
        await asyncio.wait_for(watcher, timeout=10)
    out["ezdelete_ok_ledger"] = dict(ledger.apps)
    out["ezdelete_ok_watch_cleaned"] = "ezappconfig-test-app" not in server._teardown_watchers
    sub.restore()

    # 12c. the watch warns (and exits cleanly) when the finalizer is stuck
    sub = ScriptedSubprocess([
        (0, f"{RESOURCE}/ezappconfig-test-app", ""),   # poll 1: still exists
        (0, f"{RESOURCE}/ezappconfig-test-app", ""),   # poll 2: still exists
    ]).install()
    await asyncio.wait_for(
        server._watch_teardown_completion(
            "ezappconfig-test-app", RESOURCE, 0.0), timeout=10)
    out["ezdelete_watch_stuck_done"] = True
    sub.restore()
    server._TEARDOWN_POLL_SECONDS = 10.0
    server._TEARDOWN_MAX_POLLS = 60

    # 13. delete_ezappconfig: real failure → entry KEPT (fail-safe)
    ledger = FakeLedger(apps={"ezappconfig-test-app": {}})
    server._ledger_instance = ledger
    sub = ScriptedSubprocess([(1, "", "apiserver unreachable")]).install()
    out["ezdelete_fail"] = await server.delete_ezappconfig("ezappconfig-test-app")
    out["ezdelete_fail_ledger"] = dict(ledger.apps)
    sub.restore()

    # 14. real _Ledger round-trip against a scripted kubectl (config plumbing)
    server._ledger_instance = server._Ledger()
    sub = ScriptedSubprocess([
        # initial load(): CM missing → empty ledger
        (1, "", "Error from server (NotFound): configmaps \"ezapp-deploy-ledger\" not found"),
        # record_chart's internal load(): present, empty
        (0, '{"kind": "ConfigMap", "data": {}}', ""),
        # record_chart's save(): patch ok
        (0, "configmap/ezapp-deploy-ledger patched", ""),
        # verification load(): the entry is there
        (0, '{"kind": "ConfigMap", "data": {"charts.json": '
            '"{\\"test-app/0.2.6\\": {\\"uploaded_at\\": \\"t\\", \\"filename\\": \\"\\"}}"}}', ""),
    ]).install()
    real = server._Ledger()
    empty_c, empty_a = await real.load()
    await real.record_chart("test-app", "0.2.6", "test-app-0.2.6.tgz")
    charts_after, _ = await real.load()
    sub.restore()
    out["ledger_fresh_is_empty"] = (empty_c, empty_a)
    out["ledger_roundtrip"] = charts_after
    out["ledger_patch_calls"] = [c[:3] for c in sub.calls]

    server._ledger_instance = server._Ledger()
    set_env(CHARTMUSEUM_URL=None)
    return out


r = asyncio.run(_ledger_scenarios())

check("force upload of unmanaged chart is refused",
      "refusing to overwrite" in r["force_unmanaged"].lower()
      and "bump the chart version" in r["force_unmanaged"].lower(),
      r["force_unmanaged"])
check("force refusal happens before any network call",
      r["force_unmanaged_requests"] == [], str(r["force_unmanaged_requests"]))
check("force upload of managed chart succeeds",
      "Uploaded test-app-0.2.6.tgz" in r["force_managed"], r["force_managed"])
check("force upload keeps/refreshes the ledger entry",
      "test-app/0.2.6" in r["force_managed_ledger"], str(r["force_managed_ledger"]))
check("plain upload records the ledger entry",
      "Recorded in the ownership ledger" in r["upload_201"]
      and r["upload_201_ledger"].get("test-app/0.2.6", {}).get("filename") == "test-app-0.2.6.tgz",
      f"{r['upload_201']} / {r['upload_201_ledger']}")
check("409 on unmanaged chart tells the agent to bump the version",
      "bump the chart version" in r["conflict_unmanaged"]
      and "force" not in r["conflict_unmanaged"].split(":")[1],
      r["conflict_unmanaged"])
check("409 on managed chart offers force=true",
      "force=true" in r["conflict_managed"], r["conflict_managed"])
check("delete_chart refuses unmanaged charts",
      "refusing to delete" in r["delete_unmanaged"]
      and r["delete_unmanaged_requests"] == [],
      f"{r['delete_unmanaged']} / requests={r['delete_unmanaged_requests']}")
check("delete_chart deletes a managed chart (200)",
      "Deleted test-app-0.2.6" in r["delete_ok"], r["delete_ok"])
check("delete_chart removes the ledger entry",
      "test-app/0.2.6" not in r["delete_ok_ledger"], str(r["delete_ok_ledger"]))
check("delete_chart on an already-gone chart clears the stale entry",
      "already gone" in r["delete_gone"] and "test-app/0.2.6" not in r["delete_gone_ledger"],
      f"{r['delete_gone']} / {r['delete_gone_ledger']}")
check("fresh apply: get runs first, then apply, ledger records",
      r["apply_fresh_calls"] == [["kubectl", "get"], ["kubectl", "apply"]]
      and "Recorded in the ownership ledger" in r["apply_fresh"]
      and "ezappconfig-test-app" in r["apply_fresh_ledger"],
      f"{r['apply_fresh']} / {r['apply_fresh_ledger']} / {r['apply_fresh_calls']}")
check("apply refuses to overwrite an unmanaged existing CR",
      "already exists but was not deployed by this server" in r["apply_unmanaged"]
      and r["apply_unmanaged_calls"] == 1,
      f"{r['apply_unmanaged']} (calls={r['apply_unmanaged_calls']})")
check("apply of an own (managed) CR is allowed (upgrade path)",
      "configured" in r["apply_upgrade"]
      and "ezappconfig-test-app" in r["apply_upgrade_ledger"], r["apply_upgrade"])
check("apply refuses when existence cannot be verified",
      "could not check for an existing EzAppConfig" in r["apply_check_fail"],
      r["apply_check_fail"])

# apply against a CR that is still terminating from a previous delete
ledger = FakeLedger()
server._ledger_instance = ledger
sub = ScriptedSubprocess([
    (0, json.dumps({"kind": "EzAppConfig",
                    "metadata": {"name": "ezappconfig-test-app",
                                 "deletionTimestamp": "2026-09-15T10:00:00Z"}}), ""),
]).install()
terminating = asyncio.run(server.apply_ezappconfig(VALID_EZAPPCONFIG))
sub.restore()
server._ledger_instance = server._Ledger()
check("apply against a terminating CR gets a clear message (not unmanaged-refusal)",
      "still terminating" in terminating and "finalizer" in terminating,
      terminating)
check("delete_ezappconfig refuses unmanaged CRs without calling kubectl",
      "refusing to delete" in r["ezdelete_unmanaged"] and r["ezdelete_unmanaged_calls"] == 0,
      r["ezdelete_unmanaged"])
check("delete_ezappconfig deletes a managed CR and forgets it",
      "deleted" in r["ezdelete_ok"]
      and "ezappconfig-test-app" not in r["ezdelete_ok_ledger"],
      f"{r['ezdelete_ok']} / {r['ezdelete_ok_ledger']}")
check("delete_ezappconfig clears stale entries on NotFound",
      "already gone" in r["ezdelete_gone"]
      and "ezappconfig-test-app" not in r["ezdelete_gone_ledger"],
      f"{r['ezdelete_gone']} / {r['ezdelete_gone_ledger']}")
check("delete accepted: single call removes the ledger entry (--wait=false)",
      "deleted" in r["ezdelete_ok"]
      and "operator teardown (finalizer) runs asynchronously" in r["ezdelete_ok"]
      and "ezappconfig-test-app" not in r["ezdelete_ok_ledger"],
      f"{r['ezdelete_ok']} / {r['ezdelete_ok_ledger']}")
check("background teardown watch ran to completion and cleaned itself up",
      r.get("ezdelete_ok_watch_cleaned") is True and r.get("ezdelete_ok") is not None,
      str(r.get("ezdelete_ok_watch_cleaned")))
check("stuck-finalizer watch path exits cleanly (warning, no hang)",
      r.get("ezdelete_watch_stuck_done") is True,
      str(r.get("ezdelete_watch_stuck_done")))
check("delete_ezappconfig keeps the ledger entry on real failures",
      "Error:" in r["ezdelete_fail"] and "ezappconfig-test-app" in r["ezdelete_fail_ledger"],
      f"{r['ezdelete_fail']} / {r['ezdelete_fail_ledger']}")
check("real ledger: missing ConfigMap reads as empty",
      r["ledger_fresh_is_empty"] == ({}, {}), str(r["ledger_fresh_is_empty"]))
check("real ledger: record_chart patches the pinned ConfigMap",
      ["kubectl", "patch", "configmap"] in r["ledger_patch_calls"]
      and r["ledger_roundtrip"].get("test-app/0.2.6") == {"uploaded_at": "t", "filename": ""},
      f"{r['ledger_patch_calls']} / {r['ledger_roundtrip']}")


# ─── 7. Web UI data layer (/api/managed) ─────────────────────────────────

print("7. Web UI data layer")

set_env(EZAPP_MCP_UI_ENABLED=None)
check("UI enabled by default", server._ui_enabled() is True)
set_env(EZAPP_MCP_UI_ENABLED="false")
check("UI can be disabled", server._ui_enabled() is False)
set_env(EZAPP_MCP_UI_ENABLED=None)

ledger = FakeLedger(
    apps={
        "app-b": {"applied_at": "2026-09-14T10:00:00+00:00", "chart": "other",
                  "chart_version": "1.0.0", "target_namespace": "ns-b"},
        "app-a": {"applied_at": "2026-09-14T09:00:00+00:00", "chart": "test-app",
                  "chart_version": "0.2.6", "target_namespace": "test-app-ns"},
    },
    charts={"test-app/0.2.6": {"uploaded_at": "2026-09-14T08:00:00+00:00",
                               "filename": "test-app-0.2.6.tgz"}},
)
server._ledger_instance = ledger
# Sorted app order: app-a first (live, ready), app-b second (deleted externally).
sub = ScriptedSubprocess([
    (0, json.dumps({
        "kind": "EzAppConfig",
        "spec": {"name": "test-app", "chartVersion": "0.2.6", "install": True,
                 "options": {"namespace": "test-app-ns"}},
        "status": {"status": "ready", "failureReason": ""},
    }), ""),
    (1, "", 'Error from server (NotFound): ezappconfigs.ezconfig.hpe.ezaf.com."app-b" not found'),
]).install()
snapshot = asyncio.run(server._managed_snapshot())
sub.restore()
server._ledger_instance = server._Ledger()

check("snapshot covers both ledger entries",
      [a["name"] for a in snapshot["apps"]] == ["app-a", "app-b"],
      str(snapshot["apps"]))
check("live status is merged for an existing CR",
      snapshot["apps"][0]["live"]["status"] == "ready"
      and snapshot["apps"][0]["live"]["target_namespace"] == "test-app-ns",
      str(snapshot["apps"][0]))
check("ledger metadata rides along (applied_at)",
      snapshot["apps"][0]["applied_at"] == "2026-09-14T09:00:00+00:00",
      str(snapshot["apps"][0]))
check("out-of-band deleted CR is reported, not fatal",
      "not found" in snapshot["apps"][1]["error"], str(snapshot["apps"][1]))
check("managed chart versions are listed with parsed name/version",
      snapshot["charts"] == [{"chart": "test-app", "version": "0.2.6",
                              "uploaded_at": "2026-09-14T08:00:00+00:00",
                              "filename": "test-app-0.2.6.tgz"}],
      str(snapshot["charts"]))
check("snapshot is self-describing (ledger name + generated_at)",
      snapshot["ledger_configmap"] == "ezapp-deploy-ledger"
      and bool(snapshot["generated_at"]), str(snapshot)[:120])


# ─── 8. ASGI composition (routing, traversal guard, API-key gate) ────────

print("8. ASGI composition")


async def _drive(app, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def _headers_of(messages):
    start = messages[0]
    return {k.decode(): v.decode() for k, v in start.get("headers", [])}


ui_app = server._UiApp(os.path.join(os.path.dirname(server.__file__), "ui"))
UI_ROOT = os.path.dirname(server.__file__)


async def _ui_scenarios():
    out = {}
    out["root_redirect"] = await _drive(ui_app, {
        "type": "http", "path": "/", "headers": []})
    out["index"] = await _drive(ui_app, {
        "type": "http", "path": "/ui/", "headers": []})
    out["app_js"] = await _drive(ui_app, {
        "type": "http", "path": "/ui/app.js", "headers": []})
    out["traversal"] = await _drive(ui_app, {
        "type": "http", "path": "/ui/../server.py", "headers": []})
    out["unknown"] = await _drive(ui_app, {
        "type": "http", "path": "/ui/nope.js", "headers": []})
    return out

ui = asyncio.run(_ui_scenarios())
check("/ redirects to /ui/",
      ui["root_redirect"][0]["status"] == 302
      and _headers_of(ui["root_redirect"]).get("location") == "/ui/",
      str(ui["root_redirect"][0]))
check("index.html served as html with CSP",
      ui["index"][0]["status"] == 200
      and _headers_of(ui["index"]).get("content-type", "").startswith("text/html")
      and b"default-src 'none'" in dict(ui["index"][0]["headers"]).get(
          b"content-security-policy", b""),
      str(ui["index"][0]["headers"]))
check("app.js served as javascript",
      ui["app_js"][0]["status"] == 200
      and _headers_of(ui["app_js"]).get("content-type", "").startswith("text/javascript"),
      str(ui["app_js"][0]))
check("path traversal (/ui/../server.py) is blocked",
      ui["traversal"][0]["status"] == 404, str(ui["traversal"][0]))
check("unknown UI paths are 404",
      ui["unknown"][0]["status"] == 404, str(ui["unknown"][0]))


async def _auth_scenarios():
    out = {}
    dummy_calls = []

    async def dummy_mcp(scope, receive, send):
        dummy_calls.append(scope.get("path"))
        body = b'{"mcp": true}'
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    authed = server._ApiKeyAuthMiddleware(server._ApiOrMcpApp(dummy_mcp))
    router = server._UiRouterApp(authed, ui_app)

    set_env(EZAPP_MCP_API_KEY="s3cret", EZAPP_MCP_UI_ENABLED=None)
    server._ledger_instance = FakeLedger()  # empty ledger → empty listing

    out["mcp_no_key"] = await _drive(router, {
        "type": "http", "path": "/mcp", "headers": []})
    out["mcp_with_key"] = await _drive(router, {
        "type": "http", "path": "/mcp",
        "headers": [(b"authorization", b"Bearer s3cret")]})
    out["api_no_key"] = await _drive(router, {
        "type": "http", "path": "/api/managed", "headers": []})
    out["api_xkey"] = await _drive(router, {
        "type": "http", "path": "/api/managed",
        "headers": [(b"x-api-key", b"s3cret")]})
    out["api_calls"] = list(dummy_calls)

    set_env(EZAPP_MCP_API_KEY=None)
    server._ledger_instance = server._Ledger()
    return out

auth = asyncio.run(_auth_scenarios())
check("unauthenticated /mcp is 401",
      auth["mcp_no_key"][0]["status"] == 401, str(auth["mcp_no_key"][0]))
check("authenticated /mcp reaches the MCP app",
      auth["api_calls"] == ["/mcp"]
      and auth["mcp_with_key"][0]["status"] == 200,
      f"calls={auth['api_calls']}")
check("unauthenticated /api/managed is 401 (same gate as /mcp)",
      auth["api_no_key"][0]["status"] == 401, str(auth["api_no_key"][0]))
check("X-API-Key also unlocks /api/managed with a JSON listing",
      auth["api_xkey"][0]["status"] == 200
      and _headers_of(auth["api_xkey"]).get("content-type") == "application/json"
      and b'"apps": []' in auth["api_xkey"][1]["body"],
      str(auth["api_xkey"][0]))


# ─── 9. Raw HTTP upload endpoint (/upload) ───────────────────────────────

print("9. Raw HTTP upload endpoint (/upload)")


def _dummy_mcp_app():
    async def dummy(scope, receive, send):
        body = b'{"mcp": true}'
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
    return dummy


RAW_TGZ = base64.b64decode(B64_CHART)


def _receive_factory(body, chunk_size=64 * 1024):
    """ASGI receive() that streams `body` in chunks (never one giant blob)."""
    if body:
        messages = [
            {"type": "http.request", "body": body[i:i + chunk_size], "more_body": True}
            for i in range(0, len(body), chunk_size)
        ]
        messages[-1]["more_body"] = False
    else:
        messages = [{"type": "http.request", "body": b"", "more_body": False}]
    iterator = iter(messages)

    async def receive():
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}

    return receive


async def _drive_body(app, scope, body):
    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, _receive_factory(body), send)
    return messages


async def _upload_scenarios():
    out = {}

    # 1. happy path: raw tgz → 200 uploaded, ledger records
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url, EZAPP_MCP_API_KEY="s3cret")
    ledger = FakeLedger()
    server._ledger_instance = ledger
    out["ok"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"", "headers": []},
        RAW_TGZ)
    out["ok_ledger"] = dict(ledger.charts)
    out["ok_requests"] = list(stub.state["requests"])
    stub.stop()

    # 2. force=true on an unmanaged chart → 403, no network call
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url)
    server._ledger_instance = FakeLedger()
    out["force_refused"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"force=true", "headers": []},
        RAW_TGZ)
    out["force_refused_requests"] = list(stub.state["requests"])
    stub.stop()

    # 3. force=true on a managed chart → 200 uploaded
    stub = _StubChartMuseum([201])
    set_env(CHARTMUSEUM_URL=stub.url)
    server._ledger_instance = FakeLedger(charts={"test-app/0.2.6": {}})
    out["force_ok"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"force=true", "headers": []},
        RAW_TGZ)
    stub.stop()

    # 4. garbage payload → 400 invalid
    set_env(CHARTMUSEUM_URL="http://127.0.0.1:1")
    server._ledger_instance = FakeLedger()
    out["garbage"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"", "headers": []},
        b"this is not gzip at all")

    # 5. empty body → 400
    out["empty"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"", "headers": []},
        b"")

    # 6. wrong method → 405
    out["get"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "GET", "path": "/upload",
         "query_string": b"", "headers": []},
        b"")

    # 7. oversized body → 413 (cap via env, chunked body reader)
    set_env(EZAPP_MCP_MAX_CHART_MB="1")
    out["too_large"] = await _drive_body(
        server._upload_http,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"", "headers": []},
        b"\x1f\x8b" + b"A" * (1024 * 1024 + 7))
    set_env(EZAPP_MCP_MAX_CHART_MB=None)

    # 8. routed through the auth middleware: no key → 401
    server._ledger_instance = FakeLedger()
    router = server._UiRouterApp(
        server._ApiKeyAuthMiddleware(server._ApiOrMcpApp(_dummy_mcp_app())),
        server._UiApp(os.path.join(os.path.dirname(server.__file__), "ui")))
    out["no_auth"] = await _drive_body(
        router,
        {"type": "http", "method": "POST", "path": "/upload",
         "query_string": b"", "headers": []},
        RAW_TGZ)

    server._ledger_instance = server._Ledger()
    set_env(CHARTMUSEUM_URL=None, EZAPP_MCP_API_KEY=None)
    return out


r = asyncio.run(_upload_scenarios())
check("raw POST /upload returns 200 uploaded + JSON",
      r["ok"][0]["status"] == 200
      and b'"status": "uploaded"' in r["ok"][1]["body"]
      and b'"chart": "test-app"' in r["ok"][1]["body"],
      str(r["ok"][0]) + (r["ok"][1]["body"][:200].decode() if len(r["ok"]) > 1 else ""))
check("/upload records the ledger entry (same pipeline)",
      r["ok_ledger"].get("test-app/0.2.6", {}).get("filename") == "http-upload",
      str(r["ok_ledger"]))
check("/upload forwards the raw bytes to ChartMuseum",
      r["ok_requests"] == [("POST", "/api/charts")], str(r["ok_requests"]))
check("force=true on unmanaged → 403 before any network call",
      r["force_refused"][0]["status"] == 403
      and b"refusing to overwrite" in r["force_refused"][1]["body"]
      and r["force_refused_requests"] == [],
      str(r["force_refused"][0]) + str(r["force_refused_requests"]))
check("force=true on managed chart uploads",
      r["force_ok"][0]["status"] == 200, str(r["force_ok"][0]))
check("non-gzip payload → 400 with reason",
      r["garbage"][0]["status"] == 400
      and b"not gzip" in r["garbage"][1]["body"], str(r["garbage"][0]))
check("empty body → 400",
      r["empty"][0]["status"] == 400, str(r["empty"][0]))
check("GET /upload → 405",
      r["get"][0]["status"] == 405, str(r["get"][0]))
check("oversized chunked body → 413",
      r["too_large"][0]["status"] == 413
      and b"byte cap" in r["too_large"][1]["body"], str(r["too_large"][0]))
check("/upload sits behind the API-key middleware",
      r["no_auth"][0]["status"] == 401, str(r["no_auth"][0]))


# ─── 10. Chunked upload tools (begin/chunk/commit with sha256 gate) ──────

print("10. Chunked upload tools")

import hashlib  # noqa: E402

# Incompressible payload: 60 KB of 'A' would gzip to ~300 bytes and defeat
# the multi-chunk assertion — random bytes keep the tgz ~60 KB (3 chunks).
BIG_TGZ = make_chart_tgz(extra_members=[("payload.bin", os.urandom(60000))])


async def _chunk_flow(data, sha=None, force=False, stub_script=(201,)):
    """Drive begin→chunks→commit; returns (commit_text, ledger, stub)."""
    stub = _StubChartMuseum(list(stub_script))
    set_env(CHARTMUSEUM_URL=stub.url)
    ledger = FakeLedger()
    server._ledger_instance = ledger
    begin = json.loads(await server.upload_chart_begin(
        len(data), sha or hashlib.sha256(data).hexdigest(), "flow.tgz"))
    resp = {}
    for i in range(begin["chunks_total"]):
        chunk = data[i * begin["chunk_bytes"]:(i + 1) * begin["chunk_bytes"]]
        resp = json.loads(await server.upload_chart_chunk(
            begin["handle"], i, base64.b64encode(chunk).decode()))
    text = await server.upload_chart_commit(begin["handle"], force=force)
    stub.stop()
    set_env(CHARTMUSEUM_URL=None)
    return text, ledger, resp


async def _chunk_error_scenarios():
    out = {}
    set_env(CHARTMUSEUM_URL="http://127.0.0.1:1")

    out["bad_sha"] = await server.upload_chart_begin(100, "nothex")
    out["bad_size"] = await server.upload_chart_begin(0, "0" * 64)
    out["oversize"] = await server.upload_chart_begin(
        server._max_chart_bytes() + 1, "0" * 64)

    # wrong seq / unknown handle / wrong chunk size / garbage b64
    begin = json.loads(await server.upload_chart_begin(100, hashlib.sha256(BIG_TGZ[:100]).hexdigest()))
    out["wrong_seq"] = await server.upload_chart_chunk(begin["handle"], 1, base64.b64encode(BIG_TGZ[:100]).decode())
    out["wrong_size"] = await server.upload_chart_chunk(begin["handle"], 0, base64.b64encode(BIG_TGZ[:50]).decode())
    out["bad_b64"] = await server.upload_chart_chunk(begin["handle"], 0, "!!!")
    out["unknown_handle"] = await server.upload_chart_chunk("dead" * 8, 0, base64.b64encode(BIG_TGZ[:100]).decode())
    out["commit_unknown"] = await server.upload_chart_commit("dead" * 8)

    # expired session: backdate created, chunk must refuse
    begin2 = json.loads(await server.upload_chart_begin(100, "0" * 64))
    server._upload_sessions[begin2["handle"]].created -= server.UPLOAD_SESSION_TTL_SECONDS + 1
    out["expired"] = await server.upload_chart_chunk(begin2["handle"], 0, base64.b64encode(BIG_TGZ[:100]).decode())

    # sha mismatch at commit discards the session
    begin3 = json.loads(await server.upload_chart_begin(100, "f" * 64))
    await server.upload_chart_chunk(begin3["handle"], 0, base64.b64encode(BIG_TGZ[:100]).decode())
    out["sha_mismatch"] = await server.upload_chart_commit(begin3["handle"])
    out["session_gone"] = begin3["handle"] not in server._upload_sessions

    # begin cap on concurrent sessions
    saved_max = server.UPLOAD_SESSION_MAX
    server.UPLOAD_SESSION_MAX = 2
    await server.upload_chart_begin(100, "0" * 64)
    await server.upload_chart_begin(100, "0" * 64)
    out["too_many"] = await server.upload_chart_begin(100, "0" * 64)
    server.UPLOAD_SESSION_MAX = saved_max
    server._upload_sessions.clear()

    set_env(CHARTMUSEUM_URL=None)
    return out

server._ledger_instance = FakeLedger()
errors = asyncio.run(_chunk_error_scenarios())
server._ledger_instance = server._Ledger()

text_ok, ledger_ok, last_chunk = asyncio.run(_chunk_flow(BIG_TGZ))
check("full chunked flow: begin → 3 chunks → commit uploads",
      "Uploaded test-app-" in text_ok and last_chunk["complete"]
      and last_chunk["chunks_total"] == 3 and last_chunk["received"] == len(BIG_TGZ),
      f"{text_ok} / {last_chunk}")
check("chunked upload lands in the ledger",
      ledger_ok.charts.get("test-app/0.2.6", {}).get("filename") == "flow.tgz",
      str(ledger_ok.charts))
check("begin rejects malformed sha256/size",
      all(e.startswith("Error:") for e in
          (errors["bad_sha"], errors["bad_size"], errors["oversize"])),
      f"{errors['bad_sha']} / {errors['bad_size']} / {errors['oversize']}")
check("chunks are strict-sequential",
      "expected chunk seq 0" in errors["wrong_seq"], errors["wrong_seq"])
check("fixed chunk sizes are enforced",
      "must be exactly 100 bytes (got 50)" in errors["wrong_size"], errors["wrong_size"])
check("garbage base64 chunk is refused",
      errors["bad_b64"].startswith("Error:"), errors["bad_b64"])
check("unknown handle is refused",
      "unknown or expired upload handle" in errors["unknown_handle"]
      and "unknown or expired upload handle" in errors["commit_unknown"],
      f"{errors['unknown_handle']} / {errors['commit_unknown']}")
check("expired session is refused",
      "session expired" in errors["expired"], errors["expired"])
check("sha256 mismatch at commit discards the session",
      "sha256 mismatch" in errors["sha_mismatch"] and errors["session_gone"],
      f"{errors['sha_mismatch']} / gone={errors['session_gone']}")
check("concurrent session cap is enforced",
      "already in progress" in errors["too_many"], errors["too_many"])

text_force, ledger_force, _ = asyncio.run(_chunk_flow(BIG_TGZ, force=True))
check("commit(force=true) refuses unmanaged charts (same gate)",
      "refusing to overwrite" in text_force, text_force)

# Registration gate: EZAPP_MCP_CHUNKED_UPLOAD_ENABLED=false drops the three
# tools from the server entirely (models cannot call unregistered tools).
set_env(EZAPP_MCP_CHUNKED_UPLOAD_ENABLED=None)
check("chunked tools registered by default",
      {"upload_chart_begin", "upload_chart_chunk", "upload_chart_commit"}
      <= {t.__name__ for t in server.mcp.tools},
      str([t.__name__ for t in server.mcp.tools]))
set_env(EZAPP_MCP_CHUNKED_UPLOAD_ENABLED="false")
check("env flag toggles the helper", server._chunked_upload_enabled() is False)
set_env(EZAPP_MCP_CHUNKED_UPLOAD_ENABLED=None)
check("helper defaults to enabled", server._chunked_upload_enabled() is True)

_GATE_SCRIPT = """
import os, sys, types
os.environ["EZAPP_MCP_CHUNKED_UPLOAD_ENABLED"] = "false"
m = types.ModuleType("mcp"); s = types.ModuleType("mcp.server")
class MS:
    def __init__(self, *a, **k):
        self.tools = []
    def tool(self, *a, **k):
        def d(fn):
            self.tools.append(fn)
            return fn
        return d
mm = types.ModuleType("mcp.server.mcpserver"); mm.MCPServer = MS
ca = types.ModuleType("mcp.server.caching")
class CH:
    def __init__(self, **k): self.__dict__.update(k)
ca.CacheHint = CH
ts = types.ModuleType("mcp.server.transport_security")
class TSS:
    def __init__(self, **k): self.__dict__.update(k)
ts.TransportSecuritySettings = TSS
m.server = s; s.mcpserver = mm; s.caching = ca; s.transport_security = ts
sys.modules.update({"mcp": m, "mcp.server": s, "mcp.server.mcpserver": mm,
                    "mcp.server.caching": ca, "mcp.server.transport_security": ts})
sys.path.insert(0, r"%(dir)s")
import server
names = {t.__name__ for t in server.mcp.tools}
chunked = {"upload_chart_begin", "upload_chart_chunk", "upload_chart_commit"}
print("CHUNKED:" + ("absent" if not (chunked & names) else "present"))
print("SINGLES:" + ("present" if "upload_chart" in names else "absent"))
""" % {"dir": os.path.dirname(server.__file__)}

gate = subprocess.run([sys.executable, "-c", _GATE_SCRIPT],
                      capture_output=True, text=True, timeout=120)
check("disabled gate drops the chunked tools (fresh import)",
      "CHUNKED:absent" in gate.stdout and "SINGLES:present" in gate.stdout,
      f"{gate.stdout} / {gate.stderr[-400:]}")

# Delete-tools registration gate (EZAPP_MCP_DELETE_ENABLED, default on)
set_env(EZAPP_MCP_DELETE_ENABLED=None)
check("delete tools registered by default",
      {"delete_chart", "delete_ezappconfig"} <= {t.__name__ for t in server.mcp.tools},
      str([t.__name__ for t in server.mcp.tools]))
set_env(EZAPP_MCP_DELETE_ENABLED="false")
check("delete flag toggles the helper", server._delete_enabled() is False)
set_env(EZAPP_MCP_DELETE_ENABLED=None)
check("delete helper defaults to enabled", server._delete_enabled() is True)

_DELETE_GATE_SCRIPT = """
import os, sys, types
os.environ["EZAPP_MCP_DELETE_ENABLED"] = "false"
m = types.ModuleType("mcp"); s = types.ModuleType("mcp.server")
class MS:
    def __init__(self, *a, **k):
        self.tools = []
    def tool(self, *a, **k):
        def d(fn):
            self.tools.append(fn)
            return fn
        return d
mm = types.ModuleType("mcp.server.mcpserver"); mm.MCPServer = MS
ca = types.ModuleType("mcp.server.caching")
class CH:
    def __init__(self, **k): self.__dict__.update(k)
ca.CacheHint = CH
ts = types.ModuleType("mcp.server.transport_security")
class TSS:
    def __init__(self, **k): self.__dict__.update(k)
ts.TransportSecuritySettings = TSS
m.server = s; s.mcpserver = mm; s.caching = ca; s.transport_security = ts
sys.modules.update({"mcp": m, "mcp.server": s, "mcp.server.mcpserver": mm,
                    "mcp.server.caching": ca, "mcp.server.transport_security": ts})
sys.path.insert(0, r"%(dir)s")
import server
names = {t.__name__ for t in server.mcp.tools}
print("DELETES:" + ("absent" if not ({"delete_chart", "delete_ezappconfig"} & names)
                    else "present"))
print("APPLY:" + ("present" if "apply_ezappconfig" in names else "absent"))
""" % {"dir": os.path.dirname(server.__file__)}

delete_gate = subprocess.run([sys.executable, "-c", _DELETE_GATE_SCRIPT],
                             capture_output=True, text=True, timeout=120)
check("disabled delete gate drops both delete tools (fresh import)",
      "DELETES:absent" in delete_gate.stdout and "APPLY:present" in delete_gate.stdout,
      f"{delete_gate.stdout} / {delete_gate.stderr[-400:]}")


# ─── 11. TLS bypass + manifest staging (/manifest → apply manifest_id) ───

print("11. TLS bypass + manifest staging")

set_env(CHARTMUSEUM_TLS_INSECURE=None, CHARTMUSEUM_CA_BUNDLE=None)
check("no TLS config → no extra curl args", server._chartmuseum_tls_argv() == [])

set_env(CHARTMUSEUM_TLS_INSECURE="true")
check("TLS_INSECURE=true → --insecure",
      server._chartmuseum_tls_argv() == ["--insecure"])

ca_path = os.path.join(os.path.dirname(server.__file__), "test-ca.pem")
with open(ca_path, "w") as f:
    f.write("-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n")
set_env(CHARTMUSEUM_TLS_INSECURE=None, CHARTMUSEUM_CA_BUNDLE=ca_path)
check("CA_BUNDLE → --cacert <path>",
      server._chartmuseum_tls_argv() == ["--cacert", ca_path])
set_env(CHARTMUSEUM_CA_BUNDLE="/nonexistent/ca.pem")
check_raises("missing CA file fails loud", "missing file",
             server._chartmuseum_tls_argv)
set_env(CHARTMUSEUM_TLS_INSECURE="true", CHARTMUSEUM_CA_BUNDLE=ca_path)
check_raises("both TLS options at once is a config error", "not both",
             server._chartmuseum_tls_argv)
os.unlink(ca_path)
set_env(CHARTMUSEUM_TLS_INSECURE=None, CHARTMUSEUM_CA_BUNDLE=None)

# End-to-end upload with the insecure flag set (plain-http stub ignores it,
# but the argv must build and the pipeline must succeed).
set_env(CHARTMUSEUM_TLS_INSECURE="true")
stub = _StubChartMuseum([201])
set_env(CHARTMUSEUM_URL=stub.url)
server._ledger_instance = FakeLedger()
tls_upload = asyncio.run(server.upload_chart(B64_CHART))
stub.stop()
set_env(CHARTMUSEUM_URL=None, CHARTMUSEUM_TLS_INSECURE=None)
server._ledger_instance = server._Ledger()
check("upload works end-to-end with --insecure configured",
      "Uploaded test-app-0.2.6.tgz" in tls_upload, tls_upload)


async def _manifest_scenarios():
    out = {}

    # staging: valid CR → 200 + id; then apply via manifest_id (server-side)
    ledger = FakeLedger()
    server._ledger_instance = ledger
    out["stage_ok"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        VALID_EZAPPCONFIG.encode())
    staged = json.loads(out["stage_ok"][1]["body"])
    sub = ScriptedSubprocess([
        (1, "", f"Error from server (NotFound): {RESOURCE}.\"ezappconfig-test-app\" not found"),
        (0, "ezappconfig.ezconfig.hpe.ezaf.com/ezappconfig-test-app created", ""),
    ]).install()
    out["apply_by_id"] = await server.apply_ezappconfig(manifest_id=staged["manifest_id"])
    out["apply_ledger"] = dict(ledger.apps)
    out["apply_calls"] = [c[:2] for c in sub.calls]
    sub.restore()

    # single-use: the id is consumed
    out["id_reuse"] = await server.apply_ezappconfig(manifest_id=staged["manifest_id"])

    # big CR through staging (the whole point: > any tool-call cap)
    big_manifest = VALID_EZAPPCONFIG.replace(
        "  description: Test app description",
        "  description: " + "A" * 150_000)
    out["stage_big"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        big_manifest.encode())
    big = json.loads(out["stage_big"][1]["body"])
    sub = ScriptedSubprocess([
        (1, "", f"Error from server (NotFound): {RESOURCE}.\"ezappconfig-test-app\" not found"),
        (0, f"{RESOURCE}/ezappconfig-test-app created", ""),
    ]).install()
    out["apply_big"] = await server.apply_ezappconfig(manifest_id=big["manifest_id"])
    sub.restore()

    # staging rejects invalid CRs up front
    out["stage_bad_kind"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: nope\n")
    out["stage_empty"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        b"")
    out["stage_get"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "GET", "path": "/manifest",
         "query_string": b"", "headers": []},
        b"")
    saved_cap = server.MAX_MANIFEST_CHARS
    server.MAX_MANIFEST_CHARS = 100
    out["stage_too_large"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        VALID_EZAPPCONFIG.encode())
    server.MAX_MANIFEST_CHARS = saved_cap

    # expired staging
    out["stage_exp"] = await _drive_body(
        server._manifest_http,
        {"type": "http", "method": "POST", "path": "/manifest",
         "query_string": b"", "headers": []},
        VALID_EZAPPCONFIG.encode())
    exp_id = json.loads(out["stage_exp"][1]["body"])["manifest_id"]
    server._staged_manifests[exp_id]["created"] -= server.MANIFEST_STAGING_TTL_SECONDS + 1
    out["apply_expired"] = await server.apply_ezappconfig(manifest_id=exp_id)

    # tool arg hygiene
    out["apply_both"] = await server.apply_ezappconfig(
        manifest_yaml=VALID_EZAPPCONFIG, manifest_id="whatever")
    out["apply_neither"] = await server.apply_ezappconfig()
    out["apply_unknown_id"] = await server.apply_ezappconfig(manifest_id="dead" * 8)

    server._ledger_instance = server._Ledger()
    return out

m = asyncio.run(_manifest_scenarios())

check("POST /manifest stages a valid CR (200 + id + name)",
      m["stage_ok"][0]["status"] == 200
      and json.loads(m["stage_ok"][1]["body"])["name"] == "ezappconfig-test-app"
      and "manifest_id" in json.loads(m["stage_ok"][1]["body"]),
      str(m["stage_ok"][0]) + m["stage_ok"][1]["body"][:200].decode())
check("apply_ezappconfig(manifest_id) applies server-side and records the ledger",
      "created" in m["apply_by_id"] and "Recorded in the ownership ledger" in m["apply_by_id"]
      and "ezappconfig-test-app" in m["apply_ledger"]
      and m["apply_calls"] == [["kubectl", "get"], ["kubectl", "apply"]],
      f"{m['apply_by_id']} / {m['apply_ledger']} / {m['apply_calls']}")
check("manifest_id is single-use",
      "unknown or expired manifest_id" in m["id_reuse"], m["id_reuse"])
check("a 150 KB CR stages and applies (no tool-call truncation possible)",
      m["stage_big"][0]["status"] == 200 and "created" in m["apply_big"],
      f"{m['stage_big'][0]['status']} / {m['apply_big'][:150]}")
check("invalid CRs are rejected at staging (400, with reason)",
      m["stage_bad_kind"][0]["status"] == 400
      and b"only kind" in m["stage_bad_kind"][1]["body"], str(m["stage_bad_kind"][0]))
check("empty body → 400", m["stage_empty"][0]["status"] == 400, str(m["stage_empty"][0]))
check("GET /manifest → 405", m["stage_get"][0]["status"] == 405, str(m["stage_get"][0]))
check("oversized manifest → 413", m["stage_too_large"][0]["status"] == 413,
      str(m["stage_too_large"][0]))
check("expired staging is refused at apply",
      "expired" in m["apply_expired"], m["apply_expired"])
check("apply rejects both/neither/unknown-id argument combos",
      all(r.startswith("Error:") for r in
          (m["apply_both"], m["apply_neither"], m["apply_unknown_id"])),
      f"{m['apply_both']} / {m['apply_neither']} / {m['apply_unknown_id']}")


# ─── 12. get_ezappconfig: summary default + blob elision ─────────────────

print("12. get_ezappconfig read-back (summary default, elided blobs)")

BIG_DOC = {
    "apiVersion": "ezconfig.hpe.ezaf.com/v1alpha1",
    "kind": "EzAppConfig",
    "metadata": {"name": "ezappconfig-test-app", "creationTimestamp": "2026-09-15T08:00:00Z",
                 "generation": 2, "resourceVersion": "123456"},
    # values blob BEFORE status, mimicking the real CR ordering that pushed
    # .status past the output truncation
    "spec": {"name": "test-app", "chartVersion": "0.2.6", "install": True,
             "values": "A" * 150_000, "logoImage": "B" * 5_000,
             "options": {"namespace": "test-app-ns"}},
    "status": {"status": "ready", "retryCnt": "0", "failureReason": ""},
}
BIG_DOC_JSON = json.dumps(BIG_DOC)


async def _get_scenarios():
    out = {}
    sub = ScriptedSubprocess([(0, BIG_DOC_JSON, "")]).install()
    out["summary"] = await server.get_ezappconfig("ezappconfig-test-app")
    sub.restore()
    sub = ScriptedSubprocess([(0, BIG_DOC_JSON, "")]).install()
    out["yaml"] = await server.get_ezappconfig("ezappconfig-test-app", output="yaml")
    sub.restore()
    sub = ScriptedSubprocess([(0, BIG_DOC_JSON, "")]).install()
    out["json"] = await server.get_ezappconfig("ezappconfig-test-app", output="json")
    sub.restore()
    sub = ScriptedSubprocess([(0, BIG_DOC_JSON, "")]).install()
    out["with_values"] = await server.get_ezappconfig(
        "ezappconfig-test-app", include_values=True)
    sub.restore()
    return out

g = asyncio.run(_get_scenarios())
summary = json.loads(g["summary"])
check("default read-back is a compact summary (not 150 KB of values)",
      len(g["summary"]) < 2_000, f"len={len(g['summary'])}")
check("summary contains the status even though it trails the values blob",
      summary["status"] == {"status": "ready", "retryCnt": "0", "failureReason": ""},
      str(summary.get("status")))
check("big blobs are elided with size+hash markers",
      "elided: 150000 bytes, sha256=" in g["summary"]
      and "elided: 5000 bytes" in g["summary"], g["summary"][:400])
check("summary carries the key spec fields",
      summary["spec"]["chartVersion"] == "0.2.6"
      and summary["spec"]["install"] is True
      and summary["spec"]["options"]["namespace"] == "test-app-ns",
      str(summary.get("spec")))
check("yaml output also elides and still parses as YAML",
      "elided: 150000 bytes" in g["yaml"] and "A" * 1000 not in g["yaml"]
      and yaml.safe_load(g["yaml"])["kind"] == "EzAppConfig",
      g["yaml"][:200])
check("json output is raw and truncates with a marker",
      "A" * 1000 in g["json"] and "truncated" in g["json"], g["json"][-120:])
check("include_values=True puts the values back (then truncates)",
      "A" * 1000 in g["with_values"] and "truncated" in g["with_values"],
      g["with_values"][-120:])


# ─── Summary ─────────────────────────────────────────────────────────────

print(f"\n{_PASS} passed, {_FAIL} failed")
sys.exit(1 if _FAIL else 0)
