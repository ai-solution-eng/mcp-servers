"""
LogSearch MCP Server

An MCP 2.0 server for read-only, namespace-scoped Kubernetes pod-log SEARCH —
the *logs* half of cluster observability that Prometheus cannot answer
(Prometheus sees metrics: rates, trends, alerts; this server sees what the
application actually printed: tracebacks, panics, OOM kills, crash loops).
Built for agent harnesses: fan-out pod log search with regex filtering, time
bounds, per-line provenance, and hard caps so a noisy namespace can never
blow up a context window. READ-ONLY by design — every tool is readOnlyHint,
and the RBAC it needs is pods get/list + pods/log get, nothing else.

Tools (all read-only, all return a JSON string):
  list_log_sources  pods + containers in one namespace, with restart counts
                    and age — the discovery call before searching
  get_pod_logs      raw log fetch for ONE pod (tail/since/previous-container)
  search_logs       regex search across every pod in a namespace, merged
                    chronologically with "pod/container: " provenance
  count_matches     per-pod match counts for one pattern, sorted descending
  export_matches    the EXACT search_logs pipeline, with the matched lines
                    written to <LOGSEARCH_EXPORT_ROOT>/<dest_name> — the one
                    non-read-only tool (it writes only into that operator-
                    configured directory; OPT-IN: unset env → the tool
                    refuses with setup instructions, nothing is written)

Configuration (environment variables, read lazily per call — flip policy or
caps in tests without reimporting):
  LOGSEARCH_ALLOWED_NAMESPACES  comma-separated fnmatch globs; EMPTY = DENY
                                ALL namespaces (default-deny, fleet decision
                                D8) unless LOGSEARCH_EMPTY_ALLOWS_ALL=1
  LOGSEARCH_EMPTY_ALLOWS_ALL    =1 restores the pre-D8 open default: an empty
                                allowlist means ALL namespaces are searchable
  LOGSEARCH_BLOCKED_NAMESPACES  comma-separated fnmatch globs; ALWAYS wins
                                over the allowed list
  LOGSEARCH_MAX_PODS            max pods per fan-out search (default 50)
  LOGSEARCH_MAX_LINES_PER_POD   max tail lines fetched per pod (default 1000)
  LOGSEARCH_MAX_TOTAL_LINES     default cap on merged search matches
                                (default 300)
  LOGSEARCH_MAX_LINE_CHARS      per-line char cap on search output lines
                                (default 2000; overlong lines are cut with
                                an explicit "...[truncated N chars]" marker)
  LOGSEARCH_FETCH_CONCURRENCY   bounded-semaphore width for the parallel pod
                                fan-out (default 8)
  LOGSEARCH_MAX_REGEX_CHARS     max length of a user regex before the ReDoS
                                screen refuses it (default 512)
  LOGSEARCH_WEBUI_ENABLED       serve the web UI + /api/* routes on the
                                streamable-http transport (default true)
  LOGSEARCH_EXPORT_ROOT         directory export_matches may write to; UNSET
                                (default) = export_matches refuses (opt-in).
                                Fleet convention: a path on the workbench/
                                shared PVC so agents read exports back.

User regexes are SCREENED at compile time: patterns whose backtracking can
explode ((a+)+, (a|aa)+, '(.*)*' shapes) are refused in microseconds with an
error explaining the rewrite, instead of freezing every worker under the GIL.
If the optional re2 package happens to be installed it is preferred for
matching (linear-time engine); nothing installs it and the screen runs either
way.
"""

import argparse
import ast
import asyncio
import fnmatch
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import mcp_auth
import mcp_metrics
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Configuration — lazy env reads (a test flips env vars, not module globals)
# ---------------------------------------------------------------------------

ENV_ALLOWED = "LOGSEARCH_ALLOWED_NAMESPACES"
ENV_BLOCKED = "LOGSEARCH_BLOCKED_NAMESPACES"
ENV_EMPTY_ALLOWS_ALL = "LOGSEARCH_EMPTY_ALLOWS_ALL"
ENV_WEBUI_ENABLED = "LOGSEARCH_WEBUI_ENABLED"
ENV_MAX_LINE_CHARS = "LOGSEARCH_MAX_LINE_CHARS"
ENV_FETCH_CONCURRENCY = "LOGSEARCH_FETCH_CONCURRENCY"
ENV_MAX_REGEX_CHARS = "LOGSEARCH_MAX_REGEX_CHARS"

DEFAULT_MAX_PODS = 50
DEFAULT_MAX_LINES_PER_POD = 1000
DEFAULT_MAX_TOTAL_LINES = 300
DEFAULT_MAX_LINE_CHARS = 2000
DEFAULT_FETCH_CONCURRENCY = 8
DEFAULT_MAX_REGEX_CHARS = 512

# Hard ceiling on any single log body handed to a caller, regardless of
# tail_lines: one pod can emit megabyte lines, and the MCP response is the
# agent's context window.
_MAX_OUTPUT_CHARS = 100_000

# Kubernetes log timestamps are RFC3339 with NANOSECOND precision
# (2026-09-08T00:00:00.123456789Z); match the timestamp prefix wherever it
# sits in the line. Naive (offset-less) timestamps are tolerated by the
# parser and sorted as UTC.
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _env_csv(name: str) -> list[str]:
    """Parse a comma-separated env list into clean glob patterns."""
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _max_pods() -> int:
    return _env_int("LOGSEARCH_MAX_PODS", DEFAULT_MAX_PODS)


def _max_lines_per_pod() -> int:
    return _env_int("LOGSEARCH_MAX_LINES_PER_POD", DEFAULT_MAX_LINES_PER_POD)


def _default_max_total_lines() -> int:
    return _env_int("LOGSEARCH_MAX_TOTAL_LINES", DEFAULT_MAX_TOTAL_LINES)


def _max_line_chars() -> int:
    """Per-line char cap on search output (0 or negative = disabled)."""
    return _env_int(ENV_MAX_LINE_CHARS, DEFAULT_MAX_LINE_CHARS)


def _fetch_concurrency() -> int:
    """Width of the bounded-semaphore pod fan-out (>= 1)."""
    return max(1, _env_int(ENV_FETCH_CONCURRENCY, DEFAULT_FETCH_CONCURRENCY))


def _max_regex_chars() -> int:
    """Max user-regex length before the ReDoS screen refuses it (0 = off)."""
    return _env_int(ENV_MAX_REGEX_CHARS, DEFAULT_MAX_REGEX_CHARS)


def _webui_enabled() -> bool:
    """Whether the HTTP transport should also serve the web UI + /api/* JSON
    routes (webui.py). Read lazily like every other knob so a test can flip
    it without reimporting; unset/true-ish = enabled, false-ish = the plain
    /health + /mcp surface."""
    raw = os.getenv(ENV_WEBUI_ENABLED, "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _empty_allows_all() -> bool:
    """The D8 escape hatch: LOGSEARCH_EMPTY_ALLOWS_ALL=1 (or true/yes/on)
    restores the pre-D8 open default where an empty allowlist means every
    namespace is searchable. Default (unset/false-ish) = default-deny."""
    return os.getenv(ENV_EMPTY_ALLOWS_ALL, "").strip().lower() in ("1", "true", "yes", "on")


def _namespace_allowed(ns: str) -> bool:
    """Pure namespace-policy predicate: blocked ALWAYS wins, then fnmatch glob
    match against the allowlist.

    Default-deny (fleet decision D8, 2026-09): an EMPTY allowlist denies every
    namespace — pod logs carry sensitive strings, so an unconfigured server
    must answer nothing rather than everything. Operators either set
    LOGSEARCH_ALLOWED_NAMESPACES explicitly or opt back into the pre-D8 open
    default with LOGSEARCH_EMPTY_ALLOWS_ALL=1.

    fnmatchcase (not fnmatch) so matching is identical on every platform —
    namespace names are DNS labels and always lowercase.
    """
    for pattern in _env_csv(ENV_BLOCKED):
        if fnmatch.fnmatchcase(ns, pattern):
            return False
    allowed = _env_csv(ENV_ALLOWED)
    if not allowed:
        return _empty_allows_all()
    return any(fnmatch.fnmatchcase(ns, pattern) for pattern in allowed)


def _ns_denied_error(ns: str) -> str:
    """Self-describing denial: names the env vars an operator must change."""
    return (
        f"Error: namespace {ns!r} is denied by this server's namespace policy "
        f"({_ENV_POLICY_HINT})."
    )


_ENV_POLICY_HINT = (
    f"allowed={ENV_ALLOWED}, blocked={ENV_BLOCKED}; blocked always wins, an "
    f"EMPTY allowed-list DENIES all namespaces (default-deny, D8) — set the "
    f"allowlist or {ENV_EMPTY_ALLOWS_ALL}=1 to open it up"
)


class LogSearchError(Exception):
    """Self-describing failure surfaced to the caller as an 'Error:' string —
    never a traceback (pod gone, namespace gone, API down, bad regex...)."""


# ---------------------------------------------------------------------------
# Kubernetes seams — the ONLY places that touch the kubernetes client.
# Module-level functions so tests monkeypatch them directly (the test venv
# has NO kubernetes package; the import must stay lazy, inside the functions).
# ---------------------------------------------------------------------------

_core_v1_api = None


def _get_core_v1_api():
    """Lazy kubernetes import + client init, cached for the process life."""
    global _core_v1_api
    if _core_v1_api is None:
        from kubernetes import client, config  # heavy dep; absent in the test venv

        try:
            config.load_incluster_config()  # in-cluster: service account
        except config.ConfigException:
            config.load_kube_config()  # local dev / port-forwarded kubeconfig
        _core_v1_api = client.CoreV1Api()
    return _core_v1_api


def _list_pods(namespace: str, label_selector: str = "") -> list[dict]:
    """List pods in one namespace as plain dicts:
    {name, containers: [str], restarts: int, started: str}.

    restarts is the SUM over app containers (a crashed sidecar restart loop
    is triage-relevant, not noise). started is the pod start time (RFC3339)
    or '' when the API did not report one.
    """
    from kubernetes.client.rest import ApiException

    api = _get_core_v1_api()
    try:
        resp = api.list_namespaced_pod(
            namespace=namespace, label_selector=label_selector or ""
        )
    except ApiException as e:
        status = getattr(e, "status", None)
        if status == 404:
            raise LogSearchError(
                f"namespace {namespace!r} not found (404) — check the spelling "
                "or list namespaces with a cluster-wide tool."
            ) from e
        raise LogSearchError(
            f"Kubernetes API error listing pods in namespace {namespace!r} "
            f"(status {status}): {getattr(e, 'reason', '') or e}"
        ) from e
    except Exception as e:  # config load failure, connection refused, ...
        raise LogSearchError(
            f"Kubernetes cluster unreachable while listing pods in namespace "
            f"{namespace!r}: {type(e).__name__}: {e}"
        ) from e

    pods = []
    for pod in resp.items or []:
        spec = pod.spec
        status = pod.status
        containers = [c.name for c in (spec.containers or [])] if spec else []
        restarts = 0
        if status and status.container_statuses:
            restarts = sum(int(cs.restart_count or 0) for cs in status.container_statuses)
        started = ""
        if status and status.start_time:
            started = status.start_time.isoformat().replace("+00:00", "Z")
        pods.append(
            {"name": pod.metadata.name, "containers": containers, "restarts": restarts, "started": started}
        )
    return pods


def _decode_log_payload(raw) -> str:
    """Normalize a pod-log API payload to a real UTF-8 string with newlines.

    Two client quirks are handled (both found live in v0.1.0/v0.1.1):
    - raw BYTES payloads (preload=False .data) are decoded defensively;
    - the client's DEFAULT preload path returns the repr of the bytes — a
      str like "b'...\\n...'" with literal backslash-n — recovered via
      ast.literal_eval so per-line search and timestamp parsing work.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.startswith("b'") and raw.endswith("'"):
        try:
            recovered = ast.literal_eval(raw)
            if isinstance(recovered, bytes):
                return recovered.decode("utf-8", errors="replace")
        except (ValueError, SyntaxError):
            pass  # a log that genuinely reads like a bytes literal — keep as-is
    return raw if isinstance(raw, str) else str(raw)


def _read_log(
    namespace: str,
    pod: str,
    container: str,
    tail_lines: int,
    since_seconds: int | None = None,
    timestamps: bool = True,
    previous: bool = False,
) -> str:
    """Read one container's log (tail-bounded). timestamps=True prepends the
    RFC3339 timestamp every search relies on for chronological ordering;
    previous=True reads the PREVIOUS (crashed/restarted) container — the
    triage path for CrashLoopBackOff, where the current container may have
    nothing but 'back-off restarting'."""
    from kubernetes.client.rest import ApiException

    api = _get_core_v1_api()
    kwargs: dict = {"tail_lines": tail_lines, "timestamps": timestamps, "previous": previous}
    if container:
        kwargs["container"] = container
    if since_seconds:
        kwargs["since_seconds"] = int(since_seconds)
    try:
        # _preload_content=False: the client's default preload path returns
        # the log as the REPR of its bytes — a str like "b'...\\n...'" with
        # literal backslash-n and zero real newlines (verified 2026-09-11
        # against a controlled API: the whole log collapsed to one line).
        # Raw bytes via .data are honest; decode them ourselves.
        resp = api.read_namespaced_pod_log(
            name=pod, namespace=namespace, _preload_content=False, **kwargs
        )
        return _decode_log_payload(resp.data)
    except ApiException as e:
        status = getattr(e, "status", None)
        if status == 404:
            raise LogSearchError(
                f"pod {pod!r} not found in namespace {namespace!r} (404) — it may "
                "have been rescheduled mid-search; use list_log_sources to refresh."
            ) from e
        if status == 400:
            raise LogSearchError(
                f"log request for pod {pod!r} in namespace {namespace!r} rejected "
                "(400): a multi-container pod needs an explicit container name — "
                "use list_log_sources to see the container list."
            ) from e
        raise LogSearchError(
            f"Kubernetes API error reading log of pod {pod!r} in namespace "
            f"{namespace!r} (status {status}): {getattr(e, 'reason', '') or e}"
        ) from e
    except Exception as e:
        raise LogSearchError(
            f"Kubernetes cluster unreachable while reading logs of pod {pod!r} "
            f"in namespace {namespace!r}: {type(e).__name__}: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

mcp = MCPServer("logsearch-mcp")

print("LogSearch MCP Server initialized:", file=sys.stderr)
print(
    f"  namespace policy: allowed={os.getenv(ENV_ALLOWED, '') or '<empty: deny-all (D8)>'} "
    f"blocked={os.getenv(ENV_BLOCKED, '') or '<none>'} "
    f"empty-allowlist-open={_empty_allows_all()} (re-read per call)",
    file=sys.stderr,
)
print(
    f"  caps: max_pods={_max_pods()} max_lines_per_pod={_max_lines_per_pod()} "
    f"max_total_lines={_default_max_total_lines()} max_line_chars={_max_line_chars()} "
    f"fetch_concurrency={_fetch_concurrency()} max_regex_chars={_max_regex_chars()}",
    file=sys.stderr,
)

_mcp_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# API-key auth (fleet pattern, shared module pcai_utils/mcp_auth.py) —
# MANDATORY per fleet decision 2026-09 (chart fails loud without the Secret).
LOGSEARCH_API_KEYS_ENV = "LOGSEARCH_API_KEYS"
AUTH_ENV_NAMES = (mcp_auth.UNIVERSAL_API_KEYS_ENV, LOGSEARCH_API_KEYS_ENV)


def _err(context: str, exc: Exception) -> str:
    """One error shape for every tool: self-describing for expected failures
    (LogSearchError), a stderr traceback plus type summary for the rest."""
    if isinstance(exc, LogSearchError):
        return f"Error: {exc}"
    traceback.print_exc(file=sys.stderr)
    return f"Error: {context} failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# ReDoS guard — compile-time screening of user-supplied regexes
# ---------------------------------------------------------------------------
# A user pattern with catastrophic backtracking (the classic nested-quantifier
# bombs: '(a+)+', '(a|aa)+', '(.*)*', '(\w+\s)*') freezes the whole event loop
# under the CPython GIL while `re` backtracks — every worker, every request,
# until the process is killed. The screen below runs BEFORE compilation and
# rejects the dangerous shapes in microseconds with an error that explains the
# rewrite. When the optional `re2` package is importable it is preferred for
# matching (linear-time engine, belt-and-braces); nothing installs it and the
# screen stays in front either way. Screening parses with CPython's own sre
# parser (re._parser; sre_parse before 3.11), so "invalid regex" keeps its
# existing message shape and screen rejections are precise, not guessed.
#
# What is rejected (all in microseconds):
#   1. patterns longer than LOGSEARCH_MAX_REGEX_CHARS (bounded length);
#   2. NESTED variable quantifiers: a quantifier whose body contains another
#      variable quantifier (min != max), unless the body is anchored — pinned
#      to start with one definite character that no inner variable quantifier
#      can consume, e.g. '(?:\.\d+)*' ('.' pins every iteration, \d can never
#      eat the pin). Possessive/atomic inner groups shield their bodies;
#   3. alternation ambiguity under an unbounded quantifier: a branch that can
#      match empty ('(a?)+') or branches whose first characters overlap
#      ('(ERROR|Error)+' under case-insensitivity).
# A rejected pattern raises LogSearchError("unsafe regex ... rejected: ...")
# — never a traceback, never a hang.

try:  # Python >= 3.11 exposes the sre parser as re._parser / re._constants
    from re import _constants as _sre_c, _parser as _sre_p
except ImportError:  # pragma: no cover - Python < 3.11 fallback names
    try:
        import sre_constants as _sre_c  # type: ignore[no-redef]
        import sre_parse as _sre_p  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - screen degrades to the length cap
        _sre_c = None  # type: ignore[assignment]
        _sre_p = None  # type: ignore[assignment]

try:  # OPTIONAL: linear-time regex engine — used for matching when present
    import re2 as _re2
except ImportError:
    _re2 = None

if _sre_c is not None:  # opcodes + the unbounded-repeat sentinel
    _SRE_MAXREPEAT = _sre_c.MAXREPEAT
    _SRE_MAX_REPEAT = _sre_c.MAX_REPEAT
    _SRE_MIN_REPEAT = _sre_c.MIN_REPEAT
    _SRE_POSSESSIVE_REPEAT = getattr(_sre_c, "POSSESSIVE_REPEAT", None)
    _SRE_ATOMIC_GROUP = getattr(_sre_c, "ATOMIC_GROUP", None)
    _SRE_SUBPATTERN = _sre_c.SUBPATTERN
    _SRE_BRANCH = _sre_c.BRANCH
    _SRE_LITERAL = _sre_c.LITERAL
    _SRE_NOT_LITERAL = _sre_c.NOT_LITERAL
    _SRE_ANY = _sre_c.ANY
    _SRE_IN = _sre_c.IN
    _SRE_CATEGORY = _sre_c.CATEGORY
    _SRE_RANGE = _sre_c.RANGE
    _SRE_NEGATE = _sre_c.NEGATE
    _SRE_ASSERT = _sre_c.ASSERT
    _SRE_ASSERT_NOT = _sre_c.ASSERT_NOT
    _SRE_AT = _sre_c.AT
    _SRE_GROUPREF = _sre_c.GROUPREF
    _CATEGORY_PREDICATES = {
        _sre_c.CATEGORY_DIGIT: str.isdigit,
        _sre_c.CATEGORY_SPACE: str.isspace,
        _sre_c.CATEGORY_WORD: lambda ch: ch.isalnum() or ch == "_",
    }
    _CATEGORY_NOT_PREDICATES = {
        _sre_c.CATEGORY_NOT_DIGIT: str.isdigit,
        _sre_c.CATEGORY_NOT_SPACE: str.isspace,
        _sre_c.CATEGORY_NOT_WORD: lambda ch: ch.isalnum() or ch == "_",
    }
else:  # pragma: no cover - only on a Python without the sre parser
    _SRE_MAXREPEAT = None

# A bounded repeat this wide over a VARIABLE inner quantifier already admits
# combinatorial backtracking ('(a+){20}' partitions a long line C(n,20) ways),
# so it is screened like an unbounded one. Small fixed bounds ({2}, {3} — the
# common compressions like '(?:\d{1,3}\.){3}') stay allowed.
_LARGE_REPEAT_BOUND = 10

# Representative alphabet for the pairwise branch-overlap test: two
# non-negated character classes that share no probe are treated as disjoint.
_CLASS_PROBES = (
    "0123456789abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ \t-_.:,/=@"
)


def _screen_unsafe_error(what: str, pattern: str, reason: str) -> LogSearchError:
    return LogSearchError(
        f"unsafe regex {what} {pattern!r} rejected: {reason} The pattern is "
        "valid but its backtracking can explode on a non-matching line and "
        "freeze this server (ReDoS). Rewrite without quantifying a group "
        "that already contains a quantifier."
    )


def _same_char(a: str, b: str, ignorecase: bool) -> bool:
    return a == b or (ignorecase and a.lower() == b.lower())


def _in_item_covers(item, ch: str, ignorecase: bool) -> bool:
    """Can one element of a character class ([...]) match character ch?"""
    op, av = item
    if op == _SRE_LITERAL:
        return _same_char(chr(av), ch, ignorecase)
    if op == _SRE_RANGE:
        lo, hi = av
        codes = {ord(ch), ord(ch.lower()), ord(ch.upper())} if ignorecase else {ord(ch)}
        return any(lo <= code <= hi for code in codes)
    if op == _SRE_CATEGORY:
        pred = _CATEGORY_PREDICATES.get(av)
        return bool(pred(ch)) if pred else True  # unknown category: conservative
    return True  # unknown item shape: conservative


def _atom_covers(node, ch: str, ignorecase: bool) -> bool:
    """Can this single-character atom match character ch? Unknown/exotic
    atoms answer True (conservative — they fail the anchoring exemption)."""
    op, av = node
    if op == _SRE_LITERAL:
        return _same_char(chr(av), ch, ignorecase)
    if op == _SRE_NOT_LITERAL:
        return not _same_char(chr(av), ch, ignorecase)
    if op == _SRE_ANY:
        return True
    if op == _SRE_IN:
        items = list(av)
        negated = any(i[0] == _SRE_NEGATE for i in items)
        covered = any(_in_item_covers(i, ch, ignorecase) for i in items if i[0] != _SRE_NEGATE)
        return covered != negated if negated else covered
    if op == _SRE_CATEGORY:
        if av in _CATEGORY_PREDICATES:
            return bool(_CATEGORY_PREDICATES[av](ch))
        not_pred = _CATEGORY_NOT_PREDICATES.get(av)
        return (not not_pred(ch)) if not_pred else True
    return True  # GROUPREF, nested groups, lookarounds: conservative


def _subpattern_items(av) -> list:
    """Unwrap SUBPATTERN/ATOMIC_GROUP nodes to their item lists."""
    if isinstance(av, tuple):
        av = av[3] if len(av) == 4 else (av[1] if len(av) == 2 else av)
    return list(av)


def _count_variable_quantifiers(items) -> int:
    """Variable (min != max) quantifiers in a subtree. Possessive and atomic
    bodies are shields — a variable quantifier inside them cannot multiply
    the parse paths of an enclosing quantifier — so they are not descended."""
    count = 0
    for op, av in items:
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT):
            mn, mx, body = av
            if mn != mx:
                count += 1
            count += _count_variable_quantifiers(list(body))
        elif op == _SRE_SUBPATTERN:
            count += _count_variable_quantifiers(_subpattern_items(av))
        elif op == _SRE_BRANCH:
            count += sum(_count_variable_quantifiers(list(b)) for b in av[1])
        elif op in (_SRE_ASSERT, _SRE_ASSERT_NOT):
            count += _count_variable_quantifiers(_subpattern_items(av))
    return count


def _find_single_variable(items):
    """The one variable quantifier of a subtree known to contain exactly one,
    or None. Returns (min, max, body_items) of that quantifier."""
    for op, av in items:
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT):
            mn, mx, body = av
            body_items = list(body)
            if mn != mx:
                return (mn, mx, body_items)
            found = _find_single_variable(body_items)
            if found is not None:
                return found
        elif op == _SRE_SUBPATTERN:
            found = _find_single_variable(_subpattern_items(av))
            if found is not None:
                return found
        elif op == _SRE_BRANCH:
            for b in av[1]:
                found = _find_single_variable(list(b))
                if found is not None:
                    return found
        elif op in (_SRE_ASSERT, _SRE_ASSERT_NOT):
            found = _find_single_variable(_subpattern_items(av))
            if found is not None:
                return found
    return None


def _has_exotic_node(items) -> bool:
    """GROUPREF / lookaround / atomic groups make the anchoring analysis
    unreliable — the anchoring exemption refuses to apply around them."""
    for op, av in items:
        if op in (_SRE_GROUPREF, _SRE_ASSERT, _SRE_ASSERT_NOT, _SRE_ATOMIC_GROUP):
            return True
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT, _SRE_POSSESSIVE_REPEAT):
            if _has_exotic_node(list(av[2])):
                return True
        elif op == _SRE_SUBPATTERN:
            if _has_exotic_node(_subpattern_items(av)):
                return True
        elif op == _SRE_BRANCH:
            if any(_has_exotic_node(list(b)) for b in av[1]):
                return True
    return False


def _anchored_body_ok(body_items: list, ignorecase: bool) -> bool:
    """Anchoring exemption for the nested-quantifier rule: the quantified body
    is pinned to start with ONE definite literal character and its single
    variable quantifier consumes only characters that can never be that pin —
    every iteration then starts at the pin, iteration boundaries are forced by
    the input, and backtracking stays linear. '(?:\\.\\d+)*' qualifies ('.' is
    the pin, \\d never matches '.'); '(a+)+' does not ('a+' eats the pin)."""
    if not body_items or _has_exotic_node(body_items):
        return False
    first_op, first_av = body_items[0]
    if first_op != _SRE_LITERAL:
        return False
    pin = chr(first_av)
    rest = body_items[1:]
    if _count_variable_quantifiers(rest) != 1:
        return False
    found = _find_single_variable(rest)
    if found is None:
        return False
    _mn, _mx, var_body = found
    if len(var_body) != 1:
        return False  # multi-char inner: can straddle the pin boundary
    return not _atom_covers(var_body[0], pin, ignorecase)


def _can_be_empty(items) -> bool:
    """Can this subtree match the empty string? A SEQUENCE can only be empty
    when EVERY element can ('a?' alone yes; 'a?b' no — b consumes)."""
    for op, av in items:
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT, _SRE_POSSESSIVE_REPEAT):
            if av[0] != 0 and not _can_be_empty(list(av[2])):
                return False  # must iterate AND the body consumes
        elif op == _SRE_SUBPATTERN:
            if not _can_be_empty(_subpattern_items(av)):
                return False
        elif op == _SRE_BRANCH:
            if not any(_can_be_empty(list(b)) for b in av[1]):
                return False
        elif op not in (_SRE_ASSERT, _SRE_ASSERT_NOT, _SRE_AT):
            return False  # a consuming atom (literal, class, any, dot...)
        # lookarounds and anchors are zero-width → keep scanning
    return True


def _first_descriptor(items, ignorecase: bool):
    """What can this subtree START with? Returns ('empty', None) when it can
    match empty outright, ('unknown', None) when unanalyzable, or
    ('class', predicate) where predicate(ch) says whether ch can be first."""
    for op, av in items:
        if op == _SRE_LITERAL:
            ch = chr(av)
            return ("class", lambda c, _ch=ch: _same_char(c, _ch, ignorecase))
        if op == _SRE_NOT_LITERAL:
            ch = chr(av)
            return ("class", lambda c, _ch=ch: not _same_char(c, _ch, ignorecase))
        if op == _SRE_ANY:
            return ("unknown", None)
        if op == _SRE_IN:
            in_items = [i for i in av if i[0] != _SRE_NEGATE]
            negated = len(in_items) != len(av)
            if negated:
                return (
                    "class",
                    lambda c, _it=in_items: not any(
                        _in_item_covers(i, c, ignorecase) for i in _it
                    ),
                )
            return (
                "class",
                lambda c, _it=in_items: any(
                    _in_item_covers(i, c, ignorecase) for i in _it
                ),
            )
        if op == _SRE_CATEGORY:
            if av in _CATEGORY_PREDICATES:
                pred = _CATEGORY_PREDICATES[av]
                return ("class", lambda c: bool(pred(c)))
            return ("unknown", None)  # negated categories: conservative
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT, _SRE_POSSESSIVE_REPEAT):
            mn, _mx, body = av
            if mn == 0:
                continue  # optional atom: the next element decides
            kind, payload = _first_descriptor(list(body), ignorecase)
            if kind != "empty":
                return (kind, payload)
            continue
        if op == _SRE_SUBPATTERN:
            kind, payload = _first_descriptor(_subpattern_items(av), ignorecase)
            if kind == "empty":
                continue
            return (kind, payload)
        if op == _SRE_BRANCH:
            descs = [_first_descriptor(list(b), ignorecase) for b in av[1]]
            if any(d[0] == "unknown" for d in descs):
                return ("unknown", None)
            preds = [d[1] for d in descs if d[0] == "class"]
            if len(preds) == len(descs):
                return ("class", lambda c, _ps=preds: any(p(c) for p in _ps))
            continue  # some branches empty: the next element decides
        if op in (_SRE_ASSERT, _SRE_ASSERT_NOT, _SRE_AT):
            continue  # zero-width
        return ("unknown", None)  # GROUPREF and friends
    return ("empty", None)


def _branches_overlap(d1, d2) -> bool:
    """Pairwise branch-overlap test. Predicates already bake in the
    case-insensitivity flag at descriptor-build time; class-vs-class compares
    over a representative probe alphabet, unknown-vs-anything overlaps."""
    k1, p1 = d1
    k2, p2 = d2
    if k1 == "empty" or k2 == "empty":
        return False  # empty branches are rejected by their own rule
    if k1 == "unknown" or k2 == "unknown":
        return True  # conservative
    return any(p1(c) and p2(c) for c in _CLASS_PROBES)


def _check_branch_ambiguity(items, pattern: str, what: str, ignorecase: bool) -> None:
    """Under an unbounded (or wide) quantifier, an alternation must be
    unambiguous: no branch may match empty and no two branches may start with
    overlapping characters ('(a|aa)+', '(ERROR|Error)+' shapes)."""
    for op, av in items:
        if op != _SRE_BRANCH:
            continue
        branches = [list(b) for b in av[1]]
        for branch in branches:
            if _can_be_empty(branch):
                raise _screen_unsafe_error(
                    what,
                    pattern,
                    "an alternation branch inside the quantifier can match the "
                    "empty string ('(x|)+' / '(a?)+' shape).",
                )
        descs = [_first_descriptor(b, ignorecase) for b in branches]
        for i in range(len(descs)):
            for j in range(i + 1, len(descs)):
                if _branches_overlap(descs[i], descs[j]):
                    raise _screen_unsafe_error(
                        what,
                        pattern,
                        "alternation branches inside the quantifier start with "
                        "overlapping characters ('(a|aa)+' shape).",
                    )


def _scan_regex_items(items, under_ambiguous: bool, ignorecase: bool,
                      pattern: str, what: str) -> None:
    """Depth-first walk of the sre tree, enforcing the nested-quantifier and
    branch-ambiguity rules. under_ambiguous=True means 'somewhere inside an
    unbounded/wide quantifier'."""
    for op, av in items:
        if op in (_SRE_MAX_REPEAT, _SRE_MIN_REPEAT):
            mn, mx, body = av
            body_items = list(body)
            unbounded = mx == _SRE_MAXREPEAT
            wide = unbounded or mx >= _LARGE_REPEAT_BOUND
            if wide and _count_variable_quantifiers(body_items) > 0:
                if not _anchored_body_ok(body_items, ignorecase):
                    raise _screen_unsafe_error(
                        what,
                        pattern,
                        "a quantifier is applied to a group containing another "
                        "variable quantifier ('(a+)+', '(.*)*', '(\\\\w+\\\\s)*' "
                        "shape) — note the inner quantifier can also be made "
                        "atomic/possessive: '(?>a+)+'.",
                    )
            _scan_regex_items(body_items, under_ambiguous or wide, ignorecase,
                              pattern, what)
        elif op == _SRE_POSSESSIVE_REPEAT:
            # possessive = atomic: the body commits without backtracking, so
            # it shields the outer quantifier; still scan it for its own bombs.
            _scan_regex_items(list(av[2]), under_ambiguous, ignorecase, pattern, what)
        elif op == _SRE_ATOMIC_GROUP:
            # atomic groups shield their contents from the enclosing quantifier
            _scan_regex_items(_subpattern_items(av), False, ignorecase, pattern, what)
        elif op == _SRE_SUBPATTERN:
            _scan_regex_items(_subpattern_items(av), under_ambiguous, ignorecase,
                              pattern, what)
        elif op == _SRE_BRANCH:
            if under_ambiguous:
                _check_branch_ambiguity([(op, av)], pattern, what, ignorecase)
            for b in av[1]:
                _scan_regex_items(list(b), under_ambiguous, ignorecase, pattern, what)
        elif op in (_SRE_ASSERT, _SRE_ASSERT_NOT):
            # a bomb inside a lookaround explodes on its own when evaluated
            _scan_regex_items(_subpattern_items(av), under_ambiguous, ignorecase,
                              pattern, what)


def _screen_regex(pattern: str, what: str, flags: int) -> None:
    """Compile-time ReDoS screen — see the section comment above. Raises
    LogSearchError for refused patterns AND for genuinely invalid ones (same
    'invalid regex' message the compiler used to raise)."""
    limit = _max_regex_chars()
    if limit > 0 and len(pattern) > limit:
        raise LogSearchError(
            f"unsafe regex {what} {pattern!r} rejected: {len(pattern)} characters "
            f"exceeds LOGSEARCH_MAX_REGEX_CHARS={limit} — narrow the pattern or "
            "raise the cap."
        )
    if _sre_p is None or not isinstance(pattern, str):  # pragma: no cover
        return  # parser unavailable: the length cap is the only screen
    ignorecase = bool(flags & re.IGNORECASE)
    try:
        tree = _sre_p.parse(pattern, flags)
    except re.error as e:
        raise LogSearchError(f"invalid regex {what} {pattern!r} ({e})") from e
    except Exception:  # pragma: no cover - exotic parser failure: fail open
        return
    _scan_regex_items(list(tree), False, ignorecase, pattern, what)


def _compile_regex(pattern: str, what: str = "pattern", flags: int = 0) -> re.Pattern:
    """Screen for ReDoS, then compile. Prefers the OPTIONAL re2 engine when
    it is importable (linear-time matching); falls back to `re` when re2 is
    absent or refuses syntax that `re` accepts (backreferences) — the screen
    above is the real guard either way."""
    _screen_regex(pattern, what, flags)
    if _re2 is not None:
        try:
            return _re2.compile(pattern, flags)
        except Exception:
            pass  # re2 rejects some valid-for-re syntax (e.g. backreferences)
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        # Re-raise as LogSearchError so the CALLER sees the bad pattern and
        # why it is bad, instead of a traceback.
        raise LogSearchError(f"invalid regex {what} {pattern!r} ({e})") from e


def _since_seconds_from_minutes(since_minutes: float | None) -> int | None:
    if since_minutes is None:
        return None
    if since_minutes <= 0:
        raise LogSearchError("since_minutes must be > 0 (e.g. 30 = last half hour).")
    return max(1, int(round(since_minutes * 60)))


def _pick_container(pod: dict, container: str) -> str | None:
    """Resolve which container of a pod to read: the requested one (only when
    the pod actually has it) or the pod's first container — the Kubernetes
    default for an unspecified container. None = nothing to read."""
    containers = pod.get("containers") or []
    if container:
        return container if container in containers else None
    return containers[0] if containers else None


# Sentinel for the parallel fan-out: this pod had no readable container, so
# no fetch was attempted and the result contributes nothing.
_SKIP = object()

# Appended to a search-output line cut by the per-line char cap. The marker
# names the exact number of dropped characters so the caller can tell how
# much of the line was withheld.
_LINE_TRUNCATION_MARKER = " ...[truncated {} chars]"


def _cap_line(line: str, cap: int) -> str:
    """Per-line char cap on search output: keep the first `cap` characters and
    append an explicit truncation marker. cap <= 0 disables the cap. The
    marker is additive (a capped line may run `cap + len(marker)` wide) so
    `cap` stays the honest visible-payload budget."""
    if cap <= 0 or len(line) <= cap:
        return line
    return line[:cap] + _LINE_TRUNCATION_MARKER.format(len(line) - cap)


def _rfc3339_sort_key(line: str) -> tuple:
    """Sort key over the RFC3339 timestamp embedded in a (provenance-prefixed)
    log line: timestamped lines first, oldest → newest; lines without a
    parseable timestamp keep their relative order at the END."""
    match = _RFC3339_RE.search(line)
    if not match:
        return (1, 0.0)
    try:
        epoch = _rfc3339_to_epoch(match.group(0))
        return (0, epoch)
    except ValueError:
        return (1, 0.0)


def _rfc3339_to_epoch(ts: str) -> float:
    # Normalize the two things fromisoformat cannot take everywhere: the 'Z'
    # suffix and >6-digit fractional seconds (k8s emits nanoseconds).
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", ts.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _human_age(started: str) -> str:
    """Pod age as a compact human string, from the RFC3339 start time."""
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
    except ValueError:
        return "unknown"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Tools — every tool: policy check → offload blocking k8s calls via
# asyncio.to_thread → JSON string. All read-only.
# ---------------------------------------------------------------------------


@mcp.tool(
    title="List Log Sources",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def list_log_sources(namespace: str, label_selector: str = "") -> str:
    """List pods+containers in one namespace with restart counts and age —
    the discovery call before searching (which pods exist, which containers
    to name in search_logs/get_pod_logs). Respects the namespace policy.

    Args:
        namespace: Kubernetes namespace to inspect.
        label_selector: Optional label selector, e.g. 'app=myapp'.
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)
    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    shown = sorted(pods, key=lambda p: p["name"])[:cap]
    for pod in shown:
        pod["age"] = _human_age(pod.get("started", ""))
    return json.dumps(
        {
            "namespace": namespace,
            "label_selector": label_selector,
            "pod_count": len(pods),
            "pod_cap_applied": pod_cap_applied,
            "pods": shown,
        }
    )


@mcp.tool(
    title="Get Pod Logs",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def get_pod_logs(
    namespace: str,
    pod: str,
    container: str = "",
    tail_lines: int = 500,
    since_seconds: int | None = None,
    previous: bool = False,
) -> str:
    """Fetch the log of ONE pod's container (tail-bounded, newest last).
    previous=true reads the PREVIOUS (crashed) container — the first move
    when a pod is in CrashLoopBackOff and the current container has nothing
    to say. Output is length-capped.

    Args:
        namespace: Kubernetes namespace of the pod.
        pod: Pod name (discover with list_log_sources).
        container: Container name; empty = the pod's first (default) container.
        tail_lines: How many trailing lines to fetch (server-capped by
            LOGSEARCH_MAX_LINES_PER_POD).
        since_seconds: Optional: only lines newer than this many seconds.
        previous: Read the previous (crashed/restarted) container instead of
            the running one.
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    if tail_lines <= 0:
        return "Error: tail_lines must be > 0."
    if since_seconds is not None and since_seconds <= 0:
        return "Error: since_seconds must be > 0 when given."
    effective_tail = min(tail_lines, _max_lines_per_pod())
    try:
        target_container = container
        if not target_container:
            # Resolve the pod's default container up front so multi-container
            # pods fail into a helpful message instead of an API 400.
            pods = await asyncio.to_thread(_list_pods, namespace, "")
            match = next((p for p in pods if p["name"] == pod), None)
            if match is None:
                return (
                    f"Error: pod {pod!r} not found in namespace {namespace!r} — "
                    "use list_log_sources to discover pods."
                )
            target_container = _pick_container(match, "")
            if target_container is None:
                return f"Error: pod {pod!r} reports no containers to read."
        log_text = await asyncio.to_thread(
            _read_log,
            namespace,
            pod,
            target_container,
            effective_tail,
            since_seconds,
            True,
            previous,
        )
    except Exception as e:
        return _err("pod log fetch", e)
    # Char-cap AFTER the fetch: the tail cap bounds line count, this bounds
    # the response even when individual lines are enormous.
    truncated = False
    if len(log_text) > _MAX_OUTPUT_CHARS:
        cut = log_text.rfind("\n", 0, _MAX_OUTPUT_CHARS)
        log_text = log_text[: cut if cut > 0 else _MAX_OUTPUT_CHARS]
        truncated = True
    lines = log_text.splitlines()
    return json.dumps(
        {
            "namespace": namespace,
            "pod": pod,
            "container": target_container,
            "previous": previous,
            "tail_lines": effective_tail,
            "lines_returned": len(lines),
            "truncated": truncated,
            "log": log_text,
        }
    )


async def _search_logs_impl(
    namespace: str,
    pattern: str,
    label_selector: str = "",
    pod_regex: str = "",
    since_minutes: float | None = None,
    tail_lines: int = 200,
    case_insensitive: bool = True,
    container: str = "",
    max_total_lines: int = 300,
) -> str:
    """THE search pipeline (Wave-5 F3): every caller — the search_logs MCP
    tool AND export_matches — goes through this exact function, so caps, the
    ReDoS screen, and the namespace policy can never diverge between the two.
    Returns the search_logs JSON payload, or an 'Error: ...' string."""
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        rx = _compile_regex(pattern, flags=re.IGNORECASE if case_insensitive else 0)
        pod_rx = _compile_regex(pod_regex, what="pod_regex") if pod_regex else None
        since_seconds = _since_seconds_from_minutes(since_minutes)
    except LogSearchError as e:
        return f"Error: {e}"
    if tail_lines <= 0:
        return "Error: tail_lines must be > 0."
    if max_total_lines <= 0:
        return "Error: max_total_lines must be > 0."
    effective_tail = min(tail_lines, _max_lines_per_pod())

    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)

    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    pods = pods[:cap]
    if pod_rx is not None:
        pods = [p for p in pods if pod_rx.search(p["name"])]
    # Deterministic fan-out order: the budget stops the search mid-flight, so
    # WHICH pods get read decides what is kept — sort by name so results do
    # not depend on API return order or on arrival timing under parallelism.
    pods.sort(key=lambda p: p["name"])

    line_cap = _max_line_chars()
    concurrency = _fetch_concurrency()
    semaphore = asyncio.Semaphore(concurrency)

    async def _fetch(pod_name: str, target_container: str | None):
        """One pod's log fetch behind the bounded semaphore; a pod with no
        readable container returns the _SKIP sentinel (nothing to read)."""
        if target_container is None:
            return _SKIP
        async with semaphore:
            return await asyncio.to_thread(
                _read_log,
                namespace,
                pod_name,
                target_container,
                effective_tail,
                since_seconds,
                True,  # timestamps: what chronological merge sort runs on
                False,
            )

    matches: list[str] = []
    errors: list[str] = []
    pods_searched = 0
    pods_with_matches = 0
    pods_skipped_budget = 0
    truncated = False
    for start in range(0, len(pods), concurrency):
        if len(matches) >= max_total_lines:
            # Budget already full from an earlier wave: never schedule these
            # pods — the pull stops before the fetch, not after it.
            truncated = True
            pods_skipped_budget += len(pods) - start
            break
        wave = [(p, _pick_container(p, container)) for p in pods[start:start + concurrency]]
        results = await asyncio.gather(
            *(_fetch(p["name"], t) for p, t in wave), return_exceptions=True
        )
        # Merge strictly in pod order (wave results come back ordered), so the
        # merged stream — and the budget stop point — is identical whether the
        # fan-out ran with concurrency 1 or 50.
        for (pod, target_container), result in zip(wave, results):
            if truncated:
                # Budget full: discard in-flight results; count the pod as
                # skipped (never scheduled, or fetched but unmerged).
                pods_skipped_budget += 1
                continue
            if target_container is None:
                continue
            if isinstance(result, BaseException):
                if isinstance(result, LogSearchError):
                    # One dead pod must not sink the search: record, keep going.
                    errors.append(f"{pod['name']}: {result}")
                    continue
                raise result
            pods_searched += 1
            prefix = f"{pod['name']}/{target_container}: "
            pod_has_match = False
            for line in result.splitlines():
                if not rx.search(line):
                    continue
                if len(matches) >= max_total_lines:
                    # Global line budget reached mid-fan-out: stop pulling.
                    truncated = True
                    pods_skipped_budget += 1
                    break
                matches.append(_cap_line(prefix + line, line_cap))
                if not pod_has_match:
                    pod_has_match = True
                    pods_with_matches += 1
        if truncated:
            break

    matches.sort(key=_rfc3339_sort_key)
    return json.dumps(
        {
            "namespace": namespace,
            "pattern": pattern,
            "matches": matches,
            "match_count": len(matches),
            "pods_searched": pods_searched,
            "pods_with_matches": pods_with_matches,
            "pod_cap_applied": pod_cap_applied,
            "truncated": truncated,
            "pods_skipped_budget": pods_skipped_budget,
            "errors": errors,
        }
    )


@mcp.tool(
    title="Search Pod Logs",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def search_logs(
    namespace: str,
    pattern: str,
    label_selector: str = "",
    pod_regex: str = "",
    since_minutes: float | None = None,
    tail_lines: int = 200,
    case_insensitive: bool = True,
    container: str = "",
    max_total_lines: int = 300,
) -> str:
    """Fan-out regex search over the pods of one namespace: fetch the tail of
    each pod (timestamps on), keep matching lines, prefix each with
    'podname/container: ' provenance, merge, and sort chronologically by the
    embedded RFC3339 timestamp (untimestamped lines last). Pods are fetched in
    parallel (bounded semaphore, LOGSEARCH_FETCH_CONCURRENCY) and the global
    line budget is enforced DURING the fan-out: once max_total_lines matches
    are in hand the search stops pulling further pods (reported in
    'pods_skipped_budget', with 'truncated' true) instead of fetching
    everything and slicing afterwards. A pod straddling the stop point counts
    in BOTH 'pods_searched' (it was read) and 'pods_skipped_budget' (its
    remaining matches were dropped). Lines longer than
    LOGSEARCH_MAX_LINE_CHARS are cut with an explicit ' ...[truncated N
    chars]' marker. Empty matches are a normal empty result, not an error.

    Args:
        namespace: Kubernetes namespace to search.
        pattern: Python regex to match against log lines, e.g. 'Traceback|ERROR'.
        label_selector: Optional pod label selector, e.g. 'app=myapp'.
        pod_regex: Optional regex narrowing WHICH pods to search by name.
        since_minutes: Optional: only fetch logs newer than this many minutes.
        tail_lines: Lines fetched per pod (server-capped by
            LOGSEARCH_MAX_LINES_PER_POD).
        case_insensitive: Match pattern case-insensitively (default true).
        container: Restrict to one container name; empty = each pod's first
            (default) container.
        max_total_lines: Cap on merged matches returned (default 300).
    """
    return await _search_logs_impl(
        namespace,
        pattern,
        label_selector=label_selector,
        pod_regex=pod_regex,
        since_minutes=since_minutes,
        tail_lines=tail_lines,
        case_insensitive=case_insensitive,
        container=container,
        max_total_lines=max_total_lines,
    )


# ---------------------------------------------------------------------------
# export_matches (Wave-5 F3 — ADDITIVE, OPT-IN via LOGSEARCH_EXPORT_ROOT)
# ---------------------------------------------------------------------------
#
# Result sets that would blow a context window go to a FILE instead: the
# search runs through the EXACT _search_logs_impl pipeline above (same
# namespace policy, ReDoS screen, pod/line caps — no bypass), and the matched
# lines land under LOGSEARCH_EXPORT_ROOT.  Unset (the default) → the tool
# refuses with setup instructions and writes NOTHING.  Suggested value (see
# README): a directory on the workbench/shared PVC so an agent reads the
# export back through its workbench tools.
#
# Path safety: dest_name is a bare FILE NAME — validated BEFORE it is joined
# under the export root (no '/' or '\' separators, no '..' anywhere, no
# leading dot, no whitespace/control characters), so the write cannot escape
# the export area.  Writes are bounded by the search's own caps
# (max_total_lines matches × per-line char cap) and land atomically
# (temp file + os.replace): a reader never sees a half-written export, and a
# re-export to the same name replaces the previous file rather than
# appending.  A failed search (denied namespace, refused regex, bad args)
# writes nothing and returns the very same error string search_logs would.

ENV_EXPORT_ROOT = "LOGSEARCH_EXPORT_ROOT"

# Bare file name for an export destination: starts with a letter/digit, then
# letters/digits/dots/underscores/dashes, ≤128 chars.  Combined with the
# explicit separator/'..' rejection below this cannot traverse, cannot name a
# hidden file, and cannot carry whitespace or control characters.
_DEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _export_root() -> Path | None:
    """LOGSEARCH_EXPORT_ROOT — the only directory export_matches may write.
    UNSET/empty (the default) → None: the tool refuses (opt-in, Wave-5 F3).
    Read per call (the fleet env-re-read pattern)."""
    raw = (os.getenv(ENV_EXPORT_ROOT) or "").strip()
    if not raw:
        return None
    return Path(raw)


def _export_unconfigured_error() -> str:
    return (
        "Error: export_matches is not configured on this server — the "
        f"{ENV_EXPORT_ROOT} environment variable is unset, and the tool "
        "refuses to write anywhere until an operator opts in. Setup: set "
        f"{ENV_EXPORT_ROOT} to an absolute directory on a writable volume that "
        "agents can read back (fleet convention: a path on the workbench/shared "
        "PVC — e.g. /data/exports, which workbench workspaces can reach via "
        "WORKBENCH_SHARED_PATHS), then retry; the directory is created on "
        "first export if it does not exist."
    )


def _dest_name_error(dest_name: str) -> str | None:
    """Path-safety screen for an export destination name; None when OK."""
    if not isinstance(dest_name, str) or not dest_name.strip() or not dest_name.strip() == dest_name:
        return (
            "Error: dest_name must be a non-empty bare file name (letters, "
            "digits, '.', '_' or '-'; no path separators, no surrounding "
            "whitespace)."
        )
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return (
            f"Error: dest_name {dest_name!r} must be a bare file name — path "
            "separators and '..' sequences are refused; an export always lands "
            f"directly under {ENV_EXPORT_ROOT}."
        )
    if not _DEST_NAME_RE.match(dest_name):
        return (
            f"Error: dest_name {dest_name!r} must match {_DEST_NAME_RE.pattern} "
            "(a bare file name starting with a letter or digit — no spaces, "
            "control characters, or leading dot)."
        )
    return None


def _export_file_text(namespace: str, pattern: str, result: dict) -> str:
    """The export file body: a small #-prefixed header (query, namespace,
    timestamp, counts) followed by the matched lines VERBATIM — they are
    already provenance-prefixed and char-capped by the search pipeline.  The
    namespace/pattern fields are flattened to one line each so a hostile
    pattern cannot forge header lines."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def one_line(s: str) -> str:
        return str(s).replace("\r", "\\r").replace("\n", "\\n")

    errors = result.get("errors") or []
    header = [
        "# logsearch export_matches",
        f"# timestamp: {ts}",
        f"# namespace: {one_line(namespace)}",
        f"# pattern: {one_line(pattern)}",
        f"# matches: {result.get('match_count', 0)} (truncated: {result.get('truncated', False)})",
        (
            f"# pods_searched: {result.get('pods_searched', 0)}"
            f"  pods_with_matches: {result.get('pods_with_matches', 0)}"
            f"  pods_skipped_budget: {result.get('pods_skipped_budget', 0)}"
            f"  pod_cap_applied: {result.get('pod_cap_applied', False)}"
        ),
        "# errors: " + (json.dumps(errors) if errors else "none"),
    ]
    matches = result.get("matches") or []
    if matches:
        # a REAL blank line separates header from payload (readers split the
        # file on it; matches never start with '# ' — they carry the
        # 'pod/container: ' provenance prefix)
        return "\n".join(header) + "\n\n" + "\n".join(matches) + "\n"
    return "\n".join(header) + "\n"


@mcp.tool(
    title="Export Log Matches",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
)
async def export_matches(
    namespace: str,
    pattern: str,
    dest_name: str,
    label_selector: str = "",
    pod_regex: str = "",
    since_minutes: float | None = None,
    tail_lines: int = 200,
    case_insensitive: bool = True,
    container: str = "",
    max_total_lines: int = 300,
) -> str:
    """Run the search_logs pipeline and write the matched lines to
    `<LOGSEARCH_EXPORT_ROOT>/<dest_name>` — for result sets too big for a
    context window: the agent reads the FILE back (e.g. a workbench workspace
    that shares the export root via WORKBENCH_SHARED_PATHS) instead of the
    MCP response.  OPT-IN: with LOGSEARCH_EXPORT_ROOT unset the tool refuses
    with a self-describing setup message and writes nothing.  dest_name must
    be a bare file name (no separators, no '..'; a clear error otherwise) and
    an existing file of the same name is REPLACED (never appended).  The
    search reuses the EXACT search_logs pipeline — same namespace policy (D8),
    ReDoS screen, and caps, so the written lines are identical to what
    search_logs would return and the write is bounded by the same limits.
    The file gets a small #-prefixed header (query, namespace, timestamp,
    counts).  A failed search (bad regex, denied namespace) writes NOTHING
    and returns the same error string search_logs would.  The response
    carries counts and the file path — never the matches themselves (the
    file is the artifact).

    Args:
        namespace: Kubernetes namespace to search.
        pattern: Python regex to match against log lines, e.g. 'Traceback|ERROR'.
        dest_name: Bare file name for the export (no separators/'..'; lands
            directly under LOGSEARCH_EXPORT_ROOT).
        label_selector: Optional pod label selector, e.g. 'app=myapp'.
        pod_regex: Optional regex narrowing WHICH pods to search by name.
        since_minutes: Optional: only fetch logs newer than this many minutes.
        tail_lines: Lines fetched per pod (server-capped by
            LOGSEARCH_MAX_LINES_PER_POD).
        case_insensitive: Match pattern case-insensitively (default true).
        container: Restrict to one container name; empty = each pod's first
            (default) container.
        max_total_lines: Cap on merged matches written (default 300 — the
            same budget search_logs enforces during the fan-out).
    """
    root = _export_root()
    if root is None:
        return _export_unconfigured_error()
    dest_err = _dest_name_error(dest_name)
    if dest_err:
        return dest_err
    # The search — policy, ReDoS screen, caps — is the EXACT pipeline; an
    # 'Error: ...' result is returned verbatim and writes nothing.
    raw = await _search_logs_impl(
        namespace,
        pattern,
        label_selector=label_selector,
        pod_regex=pod_regex,
        since_minutes=since_minutes,
        tail_lines=tail_lines,
        case_insensitive=case_insensitive,
        container=container,
        max_total_lines=max_total_lines,
    )
    if raw.startswith("Error"):
        return raw
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - impl always emits JSON
        return _err("export", ValueError("search pipeline returned a non-JSON payload"))
    dest = root / dest_name
    tmp = root / f".{dest_name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(_export_file_text(namespace, pattern, result), encoding="utf-8")
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return (
            f"Error: could not write the export to {str(dest)!r} ({exc}) — "
            f"check that {ENV_EXPORT_ROOT} points at a WRITABLE directory "
            "(a mounted volume, not the read-only rootfs)."
        )
    body_bytes = dest.stat().st_size
    return json.dumps(
        {
            "exported": True,
            "path": str(dest),
            "dest_name": dest_name,
            "namespace": namespace,
            "pattern": pattern,
            "match_count": result["match_count"],
            "truncated": result["truncated"],
            "pods_searched": result["pods_searched"],
            "pods_with_matches": result["pods_with_matches"],
            "pod_cap_applied": result["pod_cap_applied"],
            "pods_skipped_budget": result["pods_skipped_budget"],
            "errors": result["errors"],
            "bytes": body_bytes,
        }
    )


@mcp.tool(
    title="Count Log Matches",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def count_matches(
    namespace: str,
    pattern: str,
    label_selector: str = "",
    since_minutes: float | None = None,
    case_insensitive: bool = True,
) -> str:
    """Per-pod match counts for one pattern across a namespace, sorted
    descending — the 'where is this error coming from?' call before reading
    full logs. Counts run over the last LOGSEARCH_MAX_LINES_PER_POD lines of
    each pod's default container, fetched in parallel (bounded semaphore,
    LOGSEARCH_FETCH_CONCURRENCY).

    Args:
        namespace: Kubernetes namespace to count in.
        pattern: Python regex to match against log lines, e.g. 'OutOfMemory'.
        label_selector: Optional pod label selector, e.g. 'app=myapp'.
        since_minutes: Optional: only fetch logs newer than this many minutes.
        case_insensitive: Match pattern case-insensitively (default true).
    """
    if not _namespace_allowed(namespace):
        return _ns_denied_error(namespace)
    try:
        rx = _compile_regex(pattern, flags=re.IGNORECASE if case_insensitive else 0)
        since_seconds = _since_seconds_from_minutes(since_minutes)
    except LogSearchError as e:
        return f"Error: {e}"

    try:
        pods = await asyncio.to_thread(_list_pods, namespace, label_selector)
    except Exception as e:
        return _err("pod listing", e)

    cap = _max_pods()
    pod_cap_applied = len(pods) > cap
    pods = pods[:cap]
    pods.sort(key=lambda p: p["name"])  # deterministic fan-out + ranking input

    concurrency = _fetch_concurrency()
    semaphore = asyncio.Semaphore(concurrency)

    async def _fetch(pod_name: str, target_container: str | None):
        if target_container is None:
            return _SKIP
        async with semaphore:
            return await asyncio.to_thread(
                _read_log,
                namespace,
                pod_name,
                target_container,
                _max_lines_per_pod(),
                since_seconds,
                True,
                False,
            )

    counts: dict[str, int] = {}
    errors: list[str] = []
    pods_searched = 0
    for start in range(0, len(pods), concurrency):
        wave = [(p, _pick_container(p, "")) for p in pods[start:start + concurrency]]
        results = await asyncio.gather(
            *(_fetch(p["name"], t) for p, t in wave), return_exceptions=True
        )
        for (pod, target_container), result in zip(wave, results):
            if target_container is None:
                continue
            if isinstance(result, BaseException):
                if isinstance(result, LogSearchError):
                    errors.append(f"{pod['name']}: {result}")
                    continue
                raise result
            pods_searched += 1
            counts[pod["name"]] = sum(
                1 for line in result.splitlines() if rx.search(line)
            )

    # Sorted descending by count (pod name breaks ties deterministically);
    # JSON preserves insertion order, so clients see the ranking directly.
    ranked = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return json.dumps(
        {
            "namespace": namespace,
            "pattern": pattern,
            "counts": ranked,
            "pods_searched": pods_searched,
            "pods_with_matches": sum(1 for v in ranked.values() if v > 0),
            "total_matches": sum(ranked.values()),
            "pod_cap_applied": pod_cap_applied,
            "errors": errors,
        }
    )


# ---------------------------------------------------------------------------
# self-metrics (Wave-3 C3 — additive, chart-gated DEFAULT-OFF)
# ---------------------------------------------------------------------------

# Count every MCP-protocol tool call (per-tool {ok,error} counters — see
# mcp_metrics.py). Unconditional and inert: the counters exist from import
# time, but nothing exposes them unless the /metrics route below is mounted,
# which requires the chart to set LOGSEARCH_METRICS_ENABLED (metrics.enabled,
# default false). No behavior change when metrics are off. Outcome labeling:
# this server's tools report failures as "Error: ..." strings (never raise),
# so a returned error string counts as outcome="error" too.
mcp_metrics.instrument(
    mcp, error_result=lambda result: isinstance(result, str) and result.startswith("Error")
)


def _metrics_enabled() -> bool:
    """Serve the /metrics endpoint? (LOGSEARCH_METRICS_ENABLED, default off).

    The chart renders this env — and the ServiceMonitor — ONLY when
    ``metrics.enabled: true`` (values), so a default deployment has no
    /metrics route at all. Read per call (env re-read, the fleet pattern)
    so tests can flip it without reimporting.
    """
    raw = os.environ.get("LOGSEARCH_METRICS_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _build_http_app():
    """Starlette app for the streamable-http transport.

    CRITICAL: the MCP session manager needs its lifespan to run (it starts
    the task group that serves /mcp requests) — even though MCP 2.0 is
    natively STATELESS (no initialize handshake, no Mcp-Session-Id, any
    replica serves any request). Mounting only the routes without
    ``lifespan=http_app.router.lifespan_context`` yields a server whose
    probes pass but where EVERY /mcp request 500s with "Task group is not
    initialized" — a real fleet bug; do not "simplify" this away.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"status": "ok", "server": "logsearch-mcp"})

    # json_response=True keeps plain-HTTP clients on single responses;
    # stateless_http=True drops session affinity entirely.
    http_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_mcp_transport_security,
    )
    routes: list = [
        Route("/health", health),
        Route("/healthz", health),
    ]
    if _metrics_enabled():
        # Prometheus self-metrics (Wave-3 C3, ADDITIVE, default OFF): per-tool
        # request counters ONLY — no namespace names, pod names, patterns, or
        # log content are exported (see mcp_metrics.py). Served key-free like
        # the probes so the ServiceMonitor can scrape it; the data is
        # non-sensitive, and the route exists only when the chart opted in
        # (metrics.enabled=true → LOGSEARCH_METRICS_ENABLED).
        routes.append(Route("/metrics", mcp_metrics.endpoint))
    if _webui_enabled():
        # HPE-branded web UI + read-only JSON API at / and /api/* — a human
        # front-end over the SAME seams/policy/caps that back the MCP tools
        # (webui.py calls the tool coroutines; it has no extra powers).
        # Gated by LOGSEARCH_WEBUI_ENABLED (helm: webui.enabled), default on.
        from webui import build_ui_routes

        routes.extend(build_ui_routes())
    routes.extend(http_app.routes)
    # API-key auth (fleet pattern, shared module: pcai_utils/mcp_auth.py).
    # Scope matches applygate: ONLY /mcp is enforced — the console's /api/*
    # is a read-only search front-end (it calls the same tool coroutines and
    # has no extra powers), and the probes must stay public for k8s. The
    # fleet decision (2026-09) makes logsearch MANDATORY-auth: the chart
    # wires the key env from an operator-created Secret and the pod fails
    # loud until it exists. One-address wiring: the UNIVERSAL MCP_API_KEYS
    # is honored alongside LOGSEARCH_API_KEYS (key sets unioned,
    # constant-time compares); comma-separated keys = the rotation story.
    return mcp_auth.ApiKeyAuthMiddleware(
        Starlette(
            routes=routes,
            lifespan=http_app.router.lifespan_context,
        ),
        env_names=AUTH_ENV_NAMES,
        protected=lambda p: p.startswith("/mcp"),
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="LogSearch MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    mcp_auth.warn_if_open("logsearch-mcp", AUTH_ENV_NAMES)

    app = _build_http_app()
    # No CORSMiddleware: the old allow_origins=["*"] let any website the
    # operator visits read pod-log search results cross-origin (fleet audit
    # S-6). The console is same-origin; MCP clients are not browsers.
    print(f"LogSearch MCP streamable-http endpoint: http://{args.host}:{args.port}/mcp")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
