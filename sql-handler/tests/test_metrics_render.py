"""P1-5 regression guard: /metrics exposition is VALID Prometheus text.

The audit found # HELP/# TYPE header pairs emitted INSIDE the per-cache
loop (three duplicate pairs per family) - Prometheus rejects duplicate
headers on the same metric family, so the whole scrape failed and
monitoring silently died. These tests parse the rendered exposition with
a minimal text-format check: every family header appears exactly once,
every sample line belongs to a declared family, and the parser round-trip
(line protocol shape) holds.

Run: python -m pytest tests/test_metrics_render.py -v
"""

import json
import re

from sqlhandler import observability


_HELP_RE = re.compile(r"^# HELP (\w+) ")
_TYPE_RE = re.compile(r"^# TYPE (\w+) (counter|gauge|histogram|summary)\s*$")
_SAMPLE_RE = re.compile(r"^(\w+)(\{[^}]*\})? ([-+0-9.eEna][^ ]*)\s*$")


def _parse(text):
    families = {}
    errors = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        m = _HELP_RE.match(line)
        if m:
            name = m.group(1)
            if name in families and families[name]["help"]:
                errors.append("line %d: duplicate # HELP for %s" % (i, name))
            families.setdefault(name, {"help": "", "type": None, "samples": 0})
            families[name]["help"] = line
            continue
        m = _TYPE_RE.match(line)
        if m:
            name = m.group(1)
            if name in families and families[name]["type"] is not None:
                errors.append("line %d: duplicate # TYPE for %s" % (i, name))
            families.setdefault(name, {"help": "", "type": None, "samples": 0})
            families[name]["type"] = m.group(2)
            continue
        if line.startswith("#"):
            errors.append("line %d: unknown comment directive: %s" % (i, line[:60]))
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            errors.append("line %d: malformed sample line: %s" % (i, line[:80]))
            continue
        metric = m.group(1)
        base = metric
        for suffix in ("_bucket", "_sum", "_count"):
            if metric.endswith(suffix) and metric[: -len(suffix)] in families:
                base = metric[: -len(suffix)]
                break
        if base in families:
            families[base]["samples"] += 1
            continue
        # histogram sub-series (metric was _bucket/_sum/_count of a base that
        # itself carries the # TYPE line under its bare name)
        if any(f.startswith(base) or base.startswith(f) for f in families):
            families.setdefault(base, {"help": "", "type": "histogram-series", "samples": 1})
            continue
        errors.append("line %d: sample for undeclared family %s" % (i, metric))
    return families, errors


def test_render_is_valid_prometheus_text_basic():
    observability.metrics.record_query("ok", 0.05, 3, "t")
    text = observability.metrics.render(None)
    families, errors = _parse(text)
    assert errors == [], errors
    for name in (
        "sqlhandler_queries_total",
        "sqlhandler_query_duration_seconds",
        "sqlhandler_query_rows_total",
    ):
        assert name in families, name
        assert families[name]["type"] is not None, name + " has no # TYPE"


def test_render_cache_gauge_headers_appear_once_per_family():
    class _Eng:
        def cache_stats(self):
            return {
                "describe_hits": 1,
                "describe_misses": 2,
                "profile_hits": 0,
                "profile_misses": 0,
                "dataset_hits": 0,
                "dataset_misses": 0,
            }

        def list_tables(self):
            return []

    text = observability.metrics.render(_Eng())
    families, errors = _parse(text)
    assert errors == [], errors
    assert text.count("# HELP sqlhandler_cache_hits_total ") == 1  # hoisted: exactly one pair per family
    assert text.count("# TYPE sqlhandler_cache_hits_total ") == 1
    assert text.count("# TYPE sqlhandler_cache_misses_total ") == 1
    assert families["sqlhandler_cache_hits_total"]["samples"] >= 3


def test_render_caller_and_write_families_valid_when_present():
    observability.metrics.record_caller_query("key")
    observability.metrics.record_write("delta", "ok")
    text = observability.metrics.render(None)
    families, errors = _parse(text)
    assert errors == [], errors
    assert families["sqlhandler_caller_queries_total"]["samples"] >= 1
    assert families["sqlhandler_writes_total"]["samples"] >= 1


def test_token_matches_non_ascii_is_401_not_500():
    assert observability.token_matches("café-token", "tok") is False
    assert observability.token_matches("tok", "tok") is True
    assert observability.token_matches("Bearer tok", "tok") is True


def test_audit_write_line_shape(tmp_path, monkeypatch):
    from sqlhandler.identity import Caller

    monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    alice = Caller(cls="user", subject="alice", key_fp="sha256:abc")
    observability.audit_write("INSERT INTO t ...", "ok", 1.0, "/srv/t", "delta", 3, None, caller=alice)
    line = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert line["event"] == "write"
    assert line["caller"] == {"class": "user", "subject": "alice", "key_fp": "sha256:abc"}
    assert line["backend"] == "delta" and line["target"] == "/srv/t"


def test_audit_query_without_caller_keeps_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    observability.audit_query("SELECT 1", "ok", 1.0, 1, None)
    line = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert "caller" not in line
