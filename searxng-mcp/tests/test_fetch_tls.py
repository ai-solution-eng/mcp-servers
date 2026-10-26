"""TLS-resilience tests for fetch_content (no network required).

Covers the corporate-egress TLS-interception fix (Zscaler-style proxies
re-sign every certificate; SE-G2 evidence 2026-10-04):

* FETCH_CA_BUNDLE — an explicit combined CA bundle passed as ``verify=`` to
  BOTH http rungs (the curl_cffi rung previously ignored verify entirely).
* FETCH_TLS_INSECURE_FALLBACK — a certificate-VERIFICATION failure retries
  ONCE with verification disabled (and marks the output); HTTP/DNS/policy
  failures never trigger it, and it never fires when disabled.
* BROWSER_IGNORE_CERT_ERRORS — the browser-side mirror knob.

Run:  .venv/bin/python -m pytest tests/test_fetch_tls.py -v
"""

import asyncio
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

import certifi
import httpx2
import pytest

import fetcher
from browser_client import BrowserClient
from fetcher import CERT_ERROR_RE, WebContentFetcher
from tests.test_searxng_mcp import SAMPLE_HTML, DummyCtx

BUNDLE = "/ca-bundle/ca-bundle.crt"

# The Zscaler Root CA — legacy X.509 encoding (basicConstraints NOT critical),
# the exact certificate Python 3.13+'s X.509-strict default rejects.
ZSCALER_ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIIE0zCCA7ugAwIBAgIJANu+mC2Jt3uTMA0GCSqGSIb3DQEBCwUAMIGhMQswCQYD
VQQGEwJVUzETMBEGA1UECBMKQ2FsaWZvcm5pYTERMA8GA1UEBxMIU2FuIEpvc2Ux
FTATBgNVBAoTDFpzY2FsZXIgSW5jLjEVMBMGA1UECxMMWnNjYWxlciBJbmMuMRgw
FgYDVQQDEw9ac2NhbGVyIFJvb3QgQ0ExIjAgBgkqhkiG9w0BCQEWE3N1cHBvcnRA
enNjYWxlci5jb20wHhcNMTQxMjE5MDAyNzU1WhcNNDIwNTA2MDAyNzU1WjCBoTEL
MAkGA1UEBhMCVVMxEzARBgNVBAgTCkNhbGlmb3JuaWExETAPBgNVBAcTCFNhbiBK
b3NlMRUwEwYDVQQKEwxac2NhbGVyIEluYy4xFTATBgNVBAsTDFpzY2FsZXIgSW5j
LjEYMBYGA1UEAxMPWnNjYWxlciBSb290IENBMSIwIAYJKoZIhvcNAQkBFhNzdXBw
b3J0QHpzY2FsZXIuY29tMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA
qT7STSxZRTgEFFf6doHajSc1vk5jmzmM6BWuOo044EsaTc9eVEV/HjH/1DWzZtcr
fTj+ni205apMTlKBW3UYR+lyLHQ9FoZiDXYXK8poKSV5+Tm0Vls/5Kb8mkhVVqv7
LgYEmvEY7HPY+i1nEGZCa46ZXCOohJ0mBEtB9JVlpDIO+nN0hUMAYYdZ1KZWCMNf
5J/aTZiShsorN2A38iSOhdd+mcRM4iNL3gsLu99XhKnRqKoHeH83lVdfu1XBeoQz
z5V6gA3kbRvhDwoIlTBeMa5l4yRdJAfdpkbFzqiwSgNdhbxTHnYYorDzKfr2rEFM
dsMU0DHdeAZf711+1CunuQIDAQABo4IBCjCCAQYwHQYDVR0OBBYEFLm33UrNww4M
hp1d3+wcBGnFTpjfMIHWBgNVHSMEgc4wgcuAFLm33UrNww4Mhp1d3+wcBGnFTpjf
oYGnpIGkMIGhMQswCQYDVQQGEwJVUzETMBEGA1UECBMKQ2FsaWZvcm5pYTERMA8G
A1UEBxMIU2FuIEpvc2UxFTATBgNVBAoTDFpzY2FsZXIgSW5jLjEVMBMGA1UECxMM
WnNjYWxlciBJbmMuMRgwFgYDVQQDEw9ac2NhbGVyIFJvb3QgQ0ExIjAgBgkqhkiG
9w0BCQEWE3N1cHBvcnRAenNjYWxlci5jb22CCQDbvpgtibd7kzAMBgNVHRMEBTAD
AQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAw0NdJh8w3NsJu4KHuVZUrmZgIohnTm0j+
RTmYQ9IKA/pvxAcA6K1i/LO+Bt+tCX+C0yxqB8qzuo+4vAzoY5JEBhyhBhf1uK+P
/WVWFZN/+hTgpSbZgzUEnWQG2gOVd24msex+0Sr7hyr9vn6OueH+jj+vCMiAm5+u
kd7lLvJsBu3AO3jGWVLyPkS3i6Gf+rwAp1OsRrv3WnbkYcFf9xjuaf4z0hRCrLN2
xFNjavxrHmsH8jPHVvgc1VD0Opja0l/BRVauTrUaoW6tE+wFG5rEcPGS80jjHK4S
pB5iDj2mUZH1T8lzYtuZy0ZPirxmtsk3135+CKNa2OCAhhFjE0xd
-----END CERTIFICATE-----
"""


# ---------------------------------------------------------------------------
# CERT_ERROR_RE — must match the incident signatures, and nothing else
# ---------------------------------------------------------------------------


def test_cert_error_regex_matches_incident_signatures():
    # The exact strings from the 2026-10-04 G2 incident (huggingface.co).
    assert CERT_ERROR_RE.search(
        "curl_cffi CertificateVerifyError: Failed to perform, curl: (60) SSL "
        "certificate problem: unable to get local issuer certificate (20)"
    )
    assert CERT_ERROR_RE.search("net::ERR_CERT_AUTHORITY_INVALID at https://example.com")
    assert CERT_ERROR_RE.search("ssl.SSLCertVerificationError: certificate verify failed")
    assert CERT_ERROR_RE.search("CERTIFICATE_VERIFY_FAILED")


def test_cert_error_regex_ignores_non_cert_failures():
    assert not CERT_ERROR_RE.search("example.com returned HTTP 503")
    assert not CERT_ERROR_RE.search("ConnectError: Connection refused")
    assert not CERT_ERROR_RE.search("ReadTimeout")
    assert not CERT_ERROR_RE.search("SSRF policy blocked navigation")


# ---------------------------------------------------------------------------
# FETCH_CA_BUNDLE — wired to verify= on both HTTP rungs
# ---------------------------------------------------------------------------


def test_ca_bundle_env_resolution(monkeypatch):
    monkeypatch.delenv("FETCH_CA_BUNDLE", raising=False)
    assert WebContentFetcher().ca_bundle is None
    monkeypatch.setenv("FETCH_CA_BUNDLE", BUNDLE)
    assert WebContentFetcher().ca_bundle == BUNDLE
    # Explicit constructor argument wins over the env (test injection).
    assert WebContentFetcher(ca_bundle="/other.pem").ca_bundle == "/other.pem"


def test_plain_rung_verify_uses_ca_bundle(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx2, "AsyncClient", FakeClient)
    bundle = _write_bundle(tmp_path)
    f = WebContentFetcher(requests_per_minute=1000, ca_bundle=str(bundle))
    f._make_client(pinned=False)
    # A configured bundle yields an explicit SSL context (strict flag cleared
    # for legacy corporate roots), not a raw path.
    ctx = captured["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert not (ctx.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))
    subjects = " ".join(str(c.get("subject", "")) for c in ctx.get_ca_certs())
    assert "Zscaler Root CA" in subjects

    # No bundle configured -> the verify_tls boolean (unchanged default).
    captured.clear()
    f2 = WebContentFetcher(requests_per_minute=1000)
    f2._make_client(pinned=False)
    assert captured["verify"] is True


def _write_bundle(tmp_path):
    """A real combined bundle file (public roots + the Zscaler root) — the
    context builder loads actual files, so tests need one on disk."""
    root = tmp_path / "zscaler-root-ca.crt"
    root.write_text(ZSCALER_ROOT_PEM)
    bundle = tmp_path / "ca-bundle.crt"
    bundle.write_text(Path(certifi.where()).read_text() + ZSCALER_ROOT_PEM)
    return bundle


def test_curl_rung_passes_verify(monkeypatch):
    """The impersonated rung must honor ca_bundle AND verify_tls (it used to
    silently ignore both — it never passed verify at all)."""
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200, headers={}, text=SAMPLE_HTML)

    import curl_cffi.requests as curl_requests

    monkeypatch.setattr(curl_requests, "get", fake_get)
    f = WebContentFetcher(requests_per_minute=1000, ca_bundle=BUNDLE)
    out = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx(), backend="curl"))
    assert "Hello World" in out
    assert captured["verify"] == BUNDLE

    captured.clear()
    f2 = WebContentFetcher(requests_per_minute=1000, verify_tls=False)
    asyncio.run(f2.fetch_and_parse("https://example.com/x", DummyCtx(), backend="curl"))
    assert captured["verify"] is False


# ---------------------------------------------------------------------------
# FETCH_TLS_INSECURE_FALLBACK — one unverified retry on cert errors only
# ---------------------------------------------------------------------------


class FakeResponse:
    status_code = 200
    is_redirect = False
    encoding = "utf-8"

    def __init__(self, body):
        self._body = body

    async def aiter_bytes(self):
        yield self._body.encode("utf-8")

    async def aclose(self):
        pass


class ScriptedClient:
    def __init__(self, *, response=None, send_exc=None):
        self._response = response
        self._send_exc = send_exc

    def build_request(self, *args, **kwargs):
        return object()

    async def send(self, request, stream=False, **kwargs):
        if self._send_exc is not None:
            raise self._send_exc
        return self._response

    async def aclose(self):
        pass


def _scripted_fetcher(monkeypatch, clients, **fetcher_kwargs):
    """WebContentFetcher whose _make_client pops from a scripted queue and
    records every RESOLVED verify= value (None from the caller means "the
    default" — the bundle SSL context, else verify_tls — exactly like the
    real client)."""
    f = WebContentFetcher(requests_per_minute=1000, **fetcher_kwargs)
    verifies = []

    def fake_make_client(*, pinned, verify=None):
        resolved = verify if verify is not None else (f._bundle_ssl_context() or f.verify_tls)
        verifies.append(resolved)
        return clients.pop(0)

    monkeypatch.setattr(f, "_make_client", fake_make_client)
    return f, verifies


def test_insecure_fallback_retries_cert_error(monkeypatch, tmp_path):
    bundle = _write_bundle(tmp_path)
    clients = [
        ScriptedClient(send_exc=Exception("CertificateVerifyError: unable to get local issuer certificate (20)")),
        ScriptedClient(response=FakeResponse(SAMPLE_HTML)),
    ]
    f, verifies = _scripted_fetcher(
        monkeypatch, clients, ca_bundle=str(bundle), insecure_tls_fallback=True
    )
    body = asyncio.run(f._get_html("https://example.com/x"))
    assert body == SAMPLE_HTML
    # First attempt verified (explicit context), retry ran unverified.
    assert isinstance(verifies[0], ssl.SSLContext)
    assert verifies[1] is False
    assert f.tls_note and "FETCH_TLS_INSECURE_FALLBACK" in f.tls_note


def test_no_fallback_when_disabled(monkeypatch, tmp_path):
    bundle = _write_bundle(tmp_path)
    clients = [
        ScriptedClient(send_exc=Exception("CertificateVerifyError: unable to get local issuer certificate (20)")),
        ScriptedClient(response=FakeResponse(SAMPLE_HTML)),
    ]
    f, verifies = _scripted_fetcher(monkeypatch, clients, ca_bundle=str(bundle))
    body = asyncio.run(f._get_html("https://example.com/x"))
    assert body is None
    assert len(verifies) == 1  # exactly one attempt, no unverified retry
    assert isinstance(verifies[0], ssl.SSLContext)


def test_no_fallback_on_http_error(monkeypatch):
    """A 5xx is a site problem, not a TLS problem — never retried unverified."""
    bad = FakeResponse(SAMPLE_HTML)
    bad.status_code = 503
    clients = [ScriptedClient(response=bad), ScriptedClient(response=FakeResponse(SAMPLE_HTML))]
    f, verifies = _scripted_fetcher(monkeypatch, clients, insecure_tls_fallback=True)
    body = asyncio.run(f._get_html("https://example.com/x"))
    assert body is None
    assert verifies == [True]  # single attempt (default trust), no retry
    assert "HTTP 503" in f.last_fetch_error


def test_curl_rung_insecure_fallback(monkeypatch):
    import curl_cffi.requests as curl_requests

    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("verify"))
        if kwargs.get("verify") is not False:
            raise Exception("curl: (60) SSL certificate problem: unable to get local issuer certificate")
        return SimpleNamespace(status_code=200, headers={}, text=SAMPLE_HTML)

    monkeypatch.setattr(curl_requests, "get", fake_get)
    f = WebContentFetcher(
        requests_per_minute=1000, ca_bundle=BUNDLE, insecure_tls_fallback=True
    )
    out = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx(), backend="curl"))
    assert "Hello World" in out
    # curl_cffi takes a bundle PATH (no context support) — unchanged contract.
    assert calls == [BUNDLE, False]
    assert f.tls_note and "FETCH_TLS_INSECURE_FALLBACK" in f.tls_note


def test_tls_note_surfaced_in_output(monkeypatch):
    f = WebContentFetcher(requests_per_minute=1000)

    async def fake_get_html(url):
        f.tls_note = (
            "TLS verification failed; the fetch retried once with "
            "verification disabled (FETCH_TLS_INSECURE_FALLBACK)"
        )
        return SAMPLE_HTML

    monkeypatch.setattr(f, "_get_html", fake_get_html)
    out = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx()))
    assert "[Note: TLS verification failed" in out
    assert "FETCH_TLS_INSECURE_FALLBACK" in out


def test_browser_note_surfaced_and_reset(monkeypatch):
    """A skipped/failed browser rung must say WHY on the success path too —
    a silently-missing screenshot cost the 2026-10-04 watch its first hour."""
    f = WebContentFetcher(requests_per_minute=1000, cache_ttl_seconds=0)

    async def fake_get_html(url):
        f.browser_note = "BrowserError: net::ERR_CERT_AUTHORITY_INVALID at https://example.com"
        return SAMPLE_HTML

    monkeypatch.setattr(f, "_get_html", fake_get_html)
    out = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx()))
    assert "[Note: headless browser not used: BrowserError: net::ERR_CERT_AUTHORITY_INVALID" in out

    # Fresh ladder run (cache off): the previous fetch's note must not leak.
    async def clean_get_html(url):
        return SAMPLE_HTML

    monkeypatch.setattr(f, "_get_html", clean_get_html)
    out2 = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx()))
    assert "headless browser not used" not in out2


def test_note_cached_with_outcome(monkeypatch):
    """A cache replay keeps the provenance of the fetch that actually ran —
    both the note's presence and the ABSENCE of a newer fetch's note."""
    f = WebContentFetcher(requests_per_minute=1000, cache_ttl_seconds=300)
    calls = {"n": 0}

    async def fake_get_html(url):
        calls["n"] += 1
        if calls["n"] == 1:
            f.browser_note = "BrowserUnavailable: sidecar disabled"
        return SAMPLE_HTML

    monkeypatch.setattr(f, "_get_html", fake_get_html)
    out1 = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx()))
    assert "headless browser not used: BrowserUnavailable" in out1
    # Cache hit: replays the SAME outcome, note included.
    out2 = asyncio.run(f.fetch_and_parse("https://example.com/x", DummyCtx()))
    assert "headless browser not used: BrowserUnavailable" in out2
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# BROWSER_IGNORE_CERT_ERRORS — browser-side mirror knob
# ---------------------------------------------------------------------------


def test_browser_ignore_cert_errors_resolution(monkeypatch):
    monkeypatch.delenv("BROWSER_IGNORE_CERT_ERRORS", raising=False)
    assert BrowserClient().ignore_cert_errors is False
    monkeypatch.setenv("BROWSER_IGNORE_CERT_ERRORS", "1")
    assert BrowserClient().ignore_cert_errors is True
    # Explicit argument wins over the env.
    assert BrowserClient(ignore_cert_errors=False).ignore_cert_errors is False


def test_browser_render_passes_ignore_https_errors(monkeypatch):
    captured = {}

    class FakePage:
        url = "https://example.com/x"

        async def goto(self, url, **kwargs):
            return None

        async def wait_for_timeout(self, ms):
            pass

        async def content(self):
            return SAMPLE_HTML

        async def title(self):
            return "T"

        async def screenshot(self, **kwargs):
            return b""

    class FakeContext:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._page = FakePage()

        async def new_page(self):
            return self._page

        async def route(self, *args, **kwargs):
            pass

        async def close(self):
            pass

    class FakeBrowser:
        def is_connected(self):
            return True

        async def new_context(self, **kwargs):
            captured["__new_context"] = True
            return FakeContext(**kwargs)

    client = BrowserClient(ignore_cert_errors=True)
    monkeypatch.setattr(client, "_browser", FakeBrowser())
    page, shot = asyncio.run(client.render("https://example.com/x"))
    assert "Hello World" in page.html
    assert captured.get("ignore_https_errors") is True

    captured.clear()
    client2 = BrowserClient()  # default: verification stays ON
    monkeypatch.setattr(client2, "_browser", FakeBrowser())
    asyncio.run(client2.render("https://example.com/x"))
    assert captured.get("ignore_https_errors") is False
