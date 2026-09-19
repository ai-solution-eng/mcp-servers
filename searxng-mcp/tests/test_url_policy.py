"""SSRF-guard tests for fetch_content (fleet audit P0-6 / decision D6).

Covers: resolved-IP denylist (with a mocked resolver), the DNS-rebinding
pin, per-hop redirect re-validation (no blind follow_redirects), the CDP /
loopback block (pre-goto + route filter), the SEARXNG_FETCH_ALLOW_HOSTS /
SEARXNG_FETCH_DENY_EXTRA escapes, the TTL cache + single-flight coalescing,
and the screenshot / response-body caps.

No network, no playwright, no extraction deps: DNS is faked at
``url_policy.resolve_host_ips``, HTTP at ``httpx2.MockTransport``, the
browser at a stub, and extraction at a stubbed trafilatura.
"""

import asyncio
import sys
import types

import httpx2
import pytest

import fetcher as fetcher_mod
import url_policy
from browser_client import BrowserClient, BrowserError, BrowserUnavailable
from fetcher import WebContentFetcher
from url_policy import UrlPolicyError, validate_fetch_url

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def set_dns(monkeypatch, mapping):
    """Fake resolution: hostname -> ip (unknown hosts fail like a real
    resolver would for a nonexistent name)."""

    def fake_resolve(host):
        if host in mapping:
            ip = mapping[host]
            return (ip,) if isinstance(ip, str) else tuple(ip)
        raise OSError(f"fake DNS: no answer for {host!r}")

    monkeypatch.setattr(url_policy, "resolve_host_ips", fake_resolve)


def no_proxy(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


class DummyCtx:
    def __init__(self):
        self.messages = []

    async def info(self, msg):
        self.messages.append(("info", msg))

    async def error(self, msg):
        self.messages.append(("error", msg))


def stub_extraction(monkeypatch, text="PARSED CONTENT"):
    """The conda test env has no trafilatura/html2text — stub the primary
    extractor so ladder tests can assert on output text."""
    monkeypatch.setattr(fetcher_mod, "extract_via_trafilatura", lambda html, url=None: text)


def make_fetcher(monkeypatch, responder, **kwargs):
    """Fetcher whose HTTP goes through a MockTransport (pinned requests and
    all — the transport sees the final request URL)."""
    no_proxy(monkeypatch)
    fetcher = WebContentFetcher(requests_per_minute=10000, **kwargs)
    base_make = fetcher._make_client

    def make_client(*, pinned):
        client = base_make(pinned=pinned)
        client._transport = httpx2.MockTransport(responder)
        return client

    monkeypatch.setattr(fetcher, "_make_client", make_client)
    return fetcher


class StubBrowser:
    """Minimal stand-in for BrowserClient (render/aclose contract)."""

    def __init__(self, page=None, shot=None, error=None):
        self.page = page
        self.shot = shot
        self.error = error
        self.calls = []

    async def render(self, url, *, screenshot=False, full_page=False):
        self.calls.append({"url": url, "screenshot": screenshot})
        if self.error is not None:
            raise self.error
        return self.page, (self.shot if screenshot else None)

    async def aclose(self):
        pass


def rendered_page(html="<html><body>rendered</body></html>", url="https://spa.example/"):
    from browser_client import RenderedPage

    return RenderedPage(html=html, final_url=url, title="t", status=200)


PUBLIC_DNS = {"example.com": "93.184.216.34", "moved.example.com": "93.184.216.34"}


# ---------------------------------------------------------------------------
# Scheme allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/html,hi",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(UrlPolicyError):
        validate_fetch_url(url)


def test_missing_scheme_keeps_historical_wording():
    with pytest.raises(UrlPolicyError, match=r"url must start with http:// or https://"):
        validate_fetch_url("example.com/no-scheme")


def test_malformed_url_is_policy_error_not_crash():
    # unclosed IPv6 bracket — urlsplit raises ValueError internally
    for bad in ("http://[::1", "http://[bogus]/"):
        with pytest.raises(UrlPolicyError, match="malformed URL"):
            validate_fetch_url(bad)
        with pytest.raises(UrlPolicyError, match="malformed URL"):
            url_policy.preflight_fetch_url(bad)


# ---------------------------------------------------------------------------
# Resolved-IP denylist (literal addresses — resolver not involved)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222/json/version",  # the pod's CDP endpoint
        "http://127.255.0.9/",  # whole 127/8
        "http://10.1.2.3/",
        "http://172.16.0.9/",
        "http://172.31.255.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[fd12::5]/",  # ULA (fc00::/7)
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped RFC1918
    ],
)
def test_literal_private_and_metadata_ips_blocked(url):
    with pytest.raises(UrlPolicyError):
        validate_fetch_url(url)


def test_metadata_hostnames_blocked():
    for url in (
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/",
        "http://metadata.azure.com/metadata/instance",
        "http://instance-data/",
    ):
        with pytest.raises(UrlPolicyError, match="metadata"):
            validate_fetch_url(url)


def test_localhost_and_cluster_local_names_blocked(monkeypatch):
    no_proxy(monkeypatch)
    for url in (
        "http://localhost:8080/search",
        "http://localhost:9222/json/list",
        "http://minio.svc:9000/",
        "http://minio.svc.cluster.local:9000/",
        "http://kubernetes.default.svc/",
        "http://api.cluster.local/",
    ):
        with pytest.raises(UrlPolicyError):
            validate_fetch_url(url)


def test_resolved_private_ip_blocked(monkeypatch):
    """A public-looking name that resolves into RFC1918 space is blocked —
    the check is on the resolved addresses (mocked resolver)."""
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"rebind.example": "10.9.9.9"})
    with pytest.raises(UrlPolicyError, match="private/internal address 10.9.9.9"):
        validate_fetch_url("http://rebind.example/")


def test_resolved_metadata_ip_blocked(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"rebind.example": "169.254.169.254"})
    with pytest.raises(UrlPolicyError, match="169.254.169.254"):
        validate_fetch_url("http://rebind.example/latest/meta-data/")


def test_unresolvable_host_blocked(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {})
    with pytest.raises(UrlPolicyError, match="could not resolve"):
        validate_fetch_url("http://nx.example/")


def test_public_host_allowed_and_pinned(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    pinned = validate_fetch_url("https://example.com/wiki/X?a=1#frag")
    assert pinned.pin_active is True
    assert pinned.ip == "93.184.216.34"
    assert pinned.pinned_url == "https://93.184.216.34/wiki/X?a=1#frag"
    assert pinned.host_header == "example.com"
    assert pinned.sni_hostname == "example.com"


def test_pinned_url_keeps_explicit_port(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"example.com": "93.184.216.34"})
    pinned = validate_fetch_url("https://example.com:8443/x")
    assert pinned.pinned_url == "https://93.184.216.34:8443/x"
    assert pinned.host_header == "example.com:8443"


def test_public_literal_ip_needs_no_dns(monkeypatch):
    no_proxy(monkeypatch)
    pinned = validate_fetch_url("http://93.184.216.34/page")
    assert pinned.pin_active and pinned.ip == "93.184.216.34"
    assert pinned.pinned_url == "http://93.184.216.34/page"


# ---------------------------------------------------------------------------
# DNS-rebinding pin at the HTTP layer
# ---------------------------------------------------------------------------


def test_fetch_connects_to_validated_ip(monkeypatch):
    """The request goes to the pinned IP with the original Host header —
    check-time resolution is what connects, not fetch-time DNS."""
    seen = []

    def responder(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("host"), dict(request.extensions)))
        return httpx2.Response(200, text="<html>ok</html>")

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    html = asyncio.run(fetcher._get_html("https://example.com/x"))
    assert html == "<html>ok</html>"
    url_seen, host_seen, ext = seen[0]
    assert url_seen == "https://93.184.216.34/x"
    assert host_seen == "example.com"
    assert ext.get("sni_hostname") == "example.com"


def test_fetch_through_proxy_keeps_hostname(monkeypatch):
    """With an HTTP(S) proxy configured the pin is skipped: the request keeps
    the original hostname (the corporate proxy performs egress DNS; the
    denylist still ran on the check-time resolution)."""
    set_dns(monkeypatch, PUBLIC_DNS)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    pinned = validate_fetch_url("https://example.com/x")
    assert pinned.pin_active is False
    assert pinned.pinned_url == "https://example.com/x"
    assert pinned.host_header == ""
    assert pinned.sni_hostname == ""


# ---------------------------------------------------------------------------
# Redirect hops: manual loop, per-hop re-validation, cap
# ---------------------------------------------------------------------------


def test_redirect_chain_followed_with_pinned_hops(monkeypatch):
    seen = []

    def responder(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("host")))
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"location": "https://moved.example.com/destination"})
        return httpx2.Response(200, text="<html>final</html>")

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    html = asyncio.run(fetcher._get_html("https://example.com/start"))
    assert html == "<html>final</html>"
    assert len(seen) == 2
    # every hop hit the IP it validated, carrying its own original host
    assert seen[0] == ("https://93.184.216.34/start", "example.com")
    assert seen[1] == ("https://93.184.216.34/destination", "moved.example.com")


def test_redirect_relative_location_resolved(monkeypatch):
    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(301, headers={"location": "/next?page=2"})
        return httpx2.Response(200, text="<html>rel</html>")

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    html = asyncio.run(fetcher._get_html("https://example.com/start"))
    assert html == "<html>rel</html>"


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:9222/json/version",  # CDP via redirect
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://localhost:8080/",
        "file:///etc/passwd",  # scheme escape via redirect
    ],
)
def test_redirect_into_blocked_space_aborts(monkeypatch, target):
    """A redirect hop into loopback/metadata/private space is re-validated
    and refused — the second request is never issued."""
    seen = []

    def responder(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(302, headers={"location": target})

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    html = asyncio.run(fetcher._get_html("https://example.com/start"))
    assert html is None
    assert len(seen) == 1  # no request to the blocked target
    assert fetcher.last_policy_error  # every blocked hop records its reason


def test_redirect_hop_cap(monkeypatch):
    count = {"n": 0}

    def responder(request: httpx2.Request) -> httpx2.Response:
        count["n"] += 1
        return httpx2.Response(302, headers={"location": "/next"})

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder, max_redirects=2)
    html = asyncio.run(fetcher._get_html("https://example.com/start"))
    assert html is None
    assert count["n"] == 3  # initial + 2 allowed hops; then abort
    assert "redirected more than 2" in fetcher.last_fetch_error


def test_policy_error_surfaces_from_ladder(monkeypatch):
    """A redirect-hop rejection reaches the tool output as a policy error —
    no browser escalation, no generic 'could not access' text."""
    stub_extraction(monkeypatch)

    def responder(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://127.0.0.1:9222/"})

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    stub = StubBrowser(page=rendered_page())
    fetcher._browser = stub
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/start", DummyCtx(), backend="auto"))
    assert out.startswith("Error: blocked by the fetch SSRF policy")
    assert "loopback" in out
    assert stub.calls == []


# ---------------------------------------------------------------------------
# Policy refusals are terminal; the "(last attempt: …)" reason stays verbatim
# (Wave-2 fix: a missing curl_cffi backend must never mangle a policy
# verdict, and a refused target is never re-requested through another rung)
# ---------------------------------------------------------------------------


def _count_impersonated(monkeypatch, fetcher):
    """Wrap the impersonated rung with a call counter (escalation detector)."""
    calls = {"impersonated": 0}
    real = fetcher._get_html_impersonated

    async def counting(url):
        calls["impersonated"] += 1
        return await real(url)

    monkeypatch.setattr(fetcher, "_get_html_impersonated", counting)
    return calls


def test_blocked_redirect_is_terminal_without_curl_cffi(monkeypatch):
    """A blocked redirect target is a terminal policy refusal: the
    impersonated rung never re-requests it, so curl_cffi's absence cannot
    overwrite the verdict — the policy reason is reported verbatim."""
    stub_extraction(monkeypatch)
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # backend absent

    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/start":
            return httpx2.Response(302, headers={"location": "http://127.0.0.1:9222/"})
        return httpx2.Response(200, text="<html>should never happen</html>")

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    calls = _count_impersonated(monkeypatch, fetcher)
    browser = StubBrowser(page=rendered_page())
    fetcher._browser = browser
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/start", DummyCtx(), backend="auto"))
    assert "127.0.0.1" in out  # the policy reason, verbatim
    assert "curl_cffi backend unavailable" not in out
    assert calls["impersonated"] == 0  # terminal: no further rung re-fetches it
    assert browser.calls == []  # the browser rung is skipped too
    assert "should never happen" not in out  # the blocked target was never fetched


def test_redirect_hop_cap_is_terminal_without_curl_cffi(monkeypatch):
    """Same for the redirect-hop cap (an SSRF-guard refusal): its reason
    reaches the output verbatim instead of the curl_cffi backend complaint
    the escalation used to leave behind."""
    stub_extraction(monkeypatch)
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # backend absent

    def responder(request: httpx2.Request) -> httpx2.Response:
        n = int(request.url.path.strip("/").split("-")[-1])
        return httpx2.Response(302, headers={"location": f"https://example.com/hop-{n + 1}"})

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder, max_redirects=5)
    calls = _count_impersonated(monkeypatch, fetcher)
    browser = StubBrowser(page=rendered_page())
    fetcher._browser = browser
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/hop-0", DummyCtx(), backend="auto"))
    assert "redirected more than 5 time(s)" in out  # the refusal reason, verbatim
    assert "curl_cffi backend unavailable" not in out
    assert calls["impersonated"] == 0
    assert browser.calls == []


def test_genuine_failure_still_reports_backend_unavailable(monkeypatch):
    """A NON-policy failure keeps today's diagnostic: with curl_cffi absent
    the '(last attempt: …)' note still names the unavailable backend."""
    stub_extraction(monkeypatch)
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # backend absent

    def responder(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection reset by peer")

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder)
    fetcher._browser = StubBrowser(error=BrowserUnavailable("sidecar not reachable"))
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/dead", DummyCtx(), backend="auto"))
    assert "Could not access the webpage" in out
    assert "curl_cffi backend unavailable" in out  # the signal is not lost


# ---------------------------------------------------------------------------
# Tool-level blocks (up-front gate) — incl. the CDP endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222/json/version",
        "http://127.0.0.1:9222/json/list",
        "http://localhost:9222/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
        "http://minio.svc.cluster.local:9000/",
    ],
)
def test_fetch_content_blocks_pod_local_targets(monkeypatch, url):
    stub = StubBrowser(page=rendered_page())
    no_proxy(monkeypatch)
    fetcher = WebContentFetcher(requests_per_minute=10000)
    fetcher._browser = stub
    out = asyncio.run(fetcher.fetch_and_parse(url, DummyCtx()))
    assert out.startswith("Error:")
    assert "SEARXNG_FETCH_ALLOW_HOSTS" in out or "never fetchable" in out
    assert stub.calls == []  # the browser rung is never reached


def test_fetch_content_blocks_non_http(monkeypatch):
    no_proxy(monkeypatch)
    fetcher = WebContentFetcher(requests_per_minute=10000)
    out = asyncio.run(fetcher.fetch_and_parse("file:///etc/passwd", DummyCtx()))
    assert out.startswith("Error: url must start with http:// or https://")


def test_allowlist_names_env_in_error(monkeypatch):
    no_proxy(monkeypatch)
    fetcher = WebContentFetcher(requests_per_minute=10000)
    out = asyncio.run(fetcher.fetch_and_parse("http://192.168.5.5/", DummyCtx()))
    assert "SEARXNG_FETCH_ALLOW_HOSTS" in out


# ---------------------------------------------------------------------------
# Escapes: SEARXNG_FETCH_ALLOW_HOSTS / SEARXNG_FETCH_DENY_EXTRA
# ---------------------------------------------------------------------------


def test_allowlist_permits_internal_target(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"minio.internal.example": "10.0.0.5"})
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", "minio.internal.example")
    pinned = validate_fetch_url("http://minio.internal.example/bucket")
    assert pinned.pin_active and pinned.ip == "10.0.0.5"
    assert pinned.pinned_url == "http://10.0.0.5/bucket"


def test_allowlist_is_additive_not_authoritative(monkeypatch):
    """Public fetching keeps working while an allowlist is configured."""
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", "minio.internal.example")
    pinned = validate_fetch_url("https://example.com/")
    assert pinned.pin_active


def test_allowlist_matches_subdomains_and_cidrs(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"a.internal.example": "10.20.30.40"})
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", ".internal.example,192.168.0.0/16")
    assert validate_fetch_url("http://a.internal.example/").pin_active


def test_allowlist_does_not_unlock_loopback_or_metadata(monkeypatch):
    no_proxy(monkeypatch)
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", "localhost,169.254.169.254,metadata")
    for url in (
        "http://localhost:9222/json",
        "http://127.0.0.1:9222/json/version",
        "http://169.254.169.254/",
        "http://metadata.google.internal/",
    ):
        with pytest.raises(UrlPolicyError):
            validate_fetch_url(url)


def test_allowlisted_private_target_still_blocked_for_others(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"a.internal.example": "10.0.0.5", "b.internal.example": "10.0.0.6"})
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", "a.internal.example")
    assert validate_fetch_url("http://a.internal.example/").pin_active
    with pytest.raises(UrlPolicyError, match="SEARXNG_FETCH_ALLOW_HOSTS"):
        validate_fetch_url("http://b.internal.example/")


def test_deny_extra_beats_allowlist(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"minio.internal.example": "10.0.0.5"})
    monkeypatch.setenv("SEARXNG_FETCH_ALLOW_HOSTS", "minio.internal.example")
    monkeypatch.setenv("SEARXNG_FETCH_DENY_EXTRA", "minio.internal.example")
    with pytest.raises(UrlPolicyError, match="SEARXNG_FETCH_DENY_EXTRA"):
        validate_fetch_url("http://minio.internal.example/")


def test_deny_extra_blocks_public_host(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    monkeypatch.setenv("SEARXNG_FETCH_DENY_EXTRA", "example.com")
    with pytest.raises(UrlPolicyError, match="SEARXNG_FETCH_DENY_EXTRA"):
        validate_fetch_url("https://example.com/")


def test_deny_extra_cidr(monkeypatch):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, {"cdn.example": "203.0.113.7"})
    monkeypatch.setenv("SEARXNG_FETCH_DENY_EXTRA", "203.0.113.0/24")
    with pytest.raises(UrlPolicyError):
        validate_fetch_url("http://cdn.example/")


# ---------------------------------------------------------------------------
# CDP guard: pre-goto validation + route filter
# ---------------------------------------------------------------------------


def test_browser_render_blocks_loopback_before_goto(monkeypatch):
    """The CDP navigation to 127.0.0.1:9222 dies at the policy gate. The
    playwright import is bypassed (monkeypatched connect) so this runs in a
    slim environment too."""
    client = BrowserClient()
    got = {}

    async def fake_connect():
        got["connected"] = True

    monkeypatch.setattr(client, "_ensure_connected", fake_connect)
    with pytest.raises(BrowserError, match="SSRF policy"):
        asyncio.run(client.render("http://127.0.0.1:9222/json/version"))
    assert got.get("connected") is True  # validation ran after connect, before goto


def test_browser_render_blocks_metadata_and_private(monkeypatch):
    client = BrowserClient()

    async def fake_connect():
        pass

    monkeypatch.setattr(client, "_ensure_connected", fake_connect)
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://10.9.8.7/",
        "http://localhost:8080/",
    ):
        with pytest.raises(BrowserError, match="SSRF policy"):
            asyncio.run(client.render(url))


class FakeRoute:
    def __init__(self, url, resource_type="document"):
        self.request = types.SimpleNamespace(url=url, resource_type=resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


def test_route_filter_aborts_pod_local_requests():
    client = BrowserClient()
    for url in (
        "http://127.0.0.1:9222/json/version",
        "http://localhost:8080/",
        "http://169.254.169.254/meta",
        "http://minio.svc/data",
        "http://10.4.4.4/x",
    ):
        route = FakeRoute(url, resource_type="fetch")
        asyncio.run(client._route_filter(route))
        assert route.aborted, url
        assert not route.continued


def test_route_filter_allows_public_and_keeps_resource_blocking():
    client = BrowserClient()
    route = FakeRoute("https://cdn.example/app.js", resource_type="script")
    asyncio.run(client._route_filter(route))
    assert route.continued and not route.aborted

    image = FakeRoute("https://cdn.example/pic.png", resource_type="image")
    asyncio.run(client._route_filter(image))
    assert image.aborted and not image.continued


def test_is_pod_local_url_matrix():
    truthy = (
        "http://127.0.0.1:9222/json",
        "http://localhost/x",
        "http://metadata.azure.com/x",
        "http://minio.svc/x",
        "http://10.0.0.1/x",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "ftp://example.com/",  # non-allowlisted scheme counts as unsafe
        "not-a-url",
    )
    falsy = ("https://cdn.example/app.js", "http://93.184.216.34/x")
    for url in truthy:
        assert url_policy.is_pod_local_url(url) is True, url
    for url in falsy:
        assert url_policy.is_pod_local_url(url) is False, url


# ---------------------------------------------------------------------------
# Quick wins: TTL cache + single-flight coalescing
# ---------------------------------------------------------------------------


def _cache_fetcher(monkeypatch, calls, delay=0.0, **kwargs):
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000, **kwargs)

    async def fake_get_html(url):
        calls["n"] += 1
        if delay:
            await asyncio.sleep(delay)
        return "<html><body>cache body</body></html>"

    monkeypatch.setattr(fetcher, "_get_html", fake_get_html)
    stub_extraction(monkeypatch)
    return fetcher


def test_cache_hit_within_ttl(monkeypatch):
    calls = {"n": 0}
    fetcher = _cache_fetcher(monkeypatch, calls)
    ctx = DummyCtx()
    out1 = asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    out2 = asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    assert calls["n"] == 1
    assert "PARSED CONTENT" in out1 and "PARSED CONTENT" in out2
    assert any("cache" in m.lower() for _, m in ctx.messages)


def test_cache_key_distinguishes_params(monkeypatch):
    calls = {"n": 0}
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000)

    async def fake_get_html(url):
        calls["n"] += 1
        return "<html><body>cache body</body></html>"

    async def fake_impersonated(url):
        calls["n"] += 1
        return "<html><body>cache body</body></html>"

    monkeypatch.setattr(fetcher, "_get_html", fake_get_html)
    monkeypatch.setattr(fetcher, "_get_html_impersonated", fake_impersonated)
    stub_extraction(monkeypatch)
    ctx = DummyCtx()
    asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="curl"))
    asyncio.run(fetcher.fetch_and_parse("https://example.com/b", ctx, backend="auto"))
    asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto", start_index=50))
    assert calls["n"] == 3  # (a, auto), (a, curl), (b, auto) — pagination hits the cache


def test_cache_ttl_zero_disables(monkeypatch):
    calls = {"n": 0}
    fetcher = _cache_fetcher(monkeypatch, calls, cache_ttl_seconds=0)
    ctx = DummyCtx()
    asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    assert calls["n"] == 2


def test_single_flight_coalesces_concurrent_fetches(monkeypatch):
    calls = {"n": 0}
    fetcher = _cache_fetcher(monkeypatch, calls, delay=0.05)

    async def run():
        return await asyncio.gather(
            fetcher.fetch_and_parse("https://example.com/a", DummyCtx(), backend="auto"),
            fetcher.fetch_and_parse("https://example.com/a", DummyCtx(), backend="auto"),
        )

    out1, out2 = asyncio.run(run())
    assert calls["n"] == 1
    assert out1 == out2


def test_failed_ladder_not_cached(monkeypatch):
    calls = {"n": 0}
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000)

    async def failing(url):
        calls["n"] += 1

    monkeypatch.setattr(fetcher, "_get_html", failing)
    ctx = DummyCtx()
    out1 = asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    out2 = asyncio.run(fetcher.fetch_and_parse("https://example.com/a", ctx, backend="auto"))
    assert calls["n"] == 2  # retried after failure, not memoized
    assert out1.startswith("Error: Could not access") == out2.startswith("Error: Could not access")


# ---------------------------------------------------------------------------
# Quick wins: response-body cap + screenshot cap / pagination contract
# ---------------------------------------------------------------------------


def test_response_body_capped_during_stream(monkeypatch):
    big = "<p>x</p>" * 100000  # ~700 KB

    def responder(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=big)

    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder, max_body_bytes=10000)
    html = asyncio.run(fetcher._get_html("https://example.com/big"))
    assert len(html) == 10000
    assert fetcher.body_truncated is True


def test_body_cap_notice_in_tool_output(monkeypatch):
    big = "<p>word </p>" * 5000

    def responder(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=big)

    stub_extraction(monkeypatch)
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = make_fetcher(monkeypatch, responder, max_body_bytes=5000)
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/big", DummyCtx(), backend="auto"))
    assert "PARSED CONTENT" in out
    assert "SEARXNG_FETCH_MAX_BODY_BYTES" in out
    assert "truncated" in out


def test_screenshot_capped_and_omission_explained(monkeypatch):
    stub_extraction(monkeypatch)
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000, max_screenshot_kb=1)
    fetcher._browser = StubBrowser(page=rendered_page(), shot="A" * 5000)  # 5 KB > 1 KB
    out = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", DummyCtx(), include_screenshot=True))
    assert "Screenshot omitted" in out
    assert "SEARXNG_FETCH_MAX_SCREENSHOT_KB" in out
    assert "data:image/png" not in out  # the oversized payload never ships


def test_screenshot_ships_with_first_page_only(monkeypatch):
    stub_extraction(monkeypatch)
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000)
    fetcher._browser = StubBrowser(page=rendered_page(), shot="QUJD")
    out1 = asyncio.run(fetcher.fetch_and_parse("https://example.com/x", DummyCtx(), include_screenshot=True))
    assert "data:image/png;base64,QUJD" in out1
    out2 = asyncio.run(
        fetcher.fetch_and_parse("https://example.com/x", DummyCtx(), include_screenshot=True, start_index=1)
    )
    assert "data:image/png;base64,QUJD" not in out2
    assert "start_index=0" in out2  # told how to get it


def test_screenshot_paginated_response_still_bounded_by_max_length(monkeypatch):
    """The old bypass: the screenshot rode AFTER the max_length slice. Now
    the only extra payload beyond the slice is the capped screenshot (page
    1) / a short note (later pages)."""
    stub_extraction(monkeypatch)
    no_proxy(monkeypatch)
    set_dns(monkeypatch, PUBLIC_DNS)
    fetcher = WebContentFetcher(requests_per_minute=10000, max_screenshot_kb=1)
    fetcher._browser = StubBrowser(page=rendered_page(), shot="A" * 2048)
    out = asyncio.run(
        fetcher.fetch_and_parse(
            "https://example.com/x",
            DummyCtx(),
            include_screenshot=True,
            max_length=50,
            start_index=0,
        )
    )
    # data-URL block absent (capped) — response stays slice-sized
    assert "data:image/png" not in out
    assert "Screenshot omitted" in out
    # a small screenshot passes on page 1
    fetcher2 = WebContentFetcher(requests_per_minute=10000)
    fetcher2._browser = StubBrowser(page=rendered_page(), shot="A" * 64)
    out2 = asyncio.run(
        fetcher2.fetch_and_parse(
            "https://example.com/x",
            DummyCtx(),
            include_screenshot=True,
            max_length=50,
            start_index=0,
        )
    )
    shot_index = out2.find("data:image/png;base64,")
    assert shot_index != -1
    # the screenshot comes after the pagination footer: page payload = slice + shot
    assert "Content info: Showing characters 0-" in out2


# ---------------------------------------------------------------------------
# Quick wins: bs4 parser preference
# ---------------------------------------------------------------------------


def test_bs4_parser_prefers_lxml_when_importable():
    try:
        import lxml  # noqa: F401

        have_lxml = True
    except ImportError:
        have_lxml = False
    parser = fetcher_mod._bs4_parser()
    assert parser == ("lxml" if have_lxml else "html.parser")


def test_bs4_parser_falls_back_without_lxml(monkeypatch):
    monkeypatch.setitem(sys.modules, "lxml", None)  # force ImportError
    assert fetcher_mod._bs4_parser() == "html.parser"


# ---------------------------------------------------------------------------
# Env wiring of the fetcher knobs
# ---------------------------------------------------------------------------


def test_fetcher_env_knobs(monkeypatch):
    no_proxy(monkeypatch)
    monkeypatch.setenv("SEARXNG_FETCH_CACHE_TTL", "7")
    monkeypatch.setenv("SEARXNG_FETCH_MAX_BODY_BYTES", "12345")
    monkeypatch.setenv("SEARXNG_FETCH_MAX_SCREENSHOT_KB", "2")
    monkeypatch.setenv("SEARXNG_FETCH_MAX_REDIRECTS", "1")
    fetcher = WebContentFetcher(requests_per_minute=10000)
    assert fetcher.result_cache.ttl_seconds == 7.0
    assert fetcher.max_body_bytes == 12345
    assert fetcher.max_screenshot_bytes == 2048
    assert fetcher.max_redirect_hops == 1


def test_fetcher_env_knobs_defaults_and_garbage(monkeypatch):
    no_proxy(monkeypatch)
    monkeypatch.setenv("SEARXNG_FETCH_CACHE_TTL", "not-a-number")
    fetcher = WebContentFetcher(requests_per_minute=10000)
    assert fetcher.result_cache.ttl_seconds == 300.0  # built-in default
    fetcher2 = WebContentFetcher(requests_per_minute=10000, cache_ttl_seconds=0)
    assert fetcher2.result_cache.ttl_seconds == 0.0
