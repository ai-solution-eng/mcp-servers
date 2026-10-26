"""SSRF-guard URL policy for ``fetch_content`` (plain + browser rungs).

Adapted from the fleet reference implementation,
``MultimodalRAG/src/multimodal_rag/utils/url_policy.py``, to this server's
threat model (fleet audit P0-6): ``fetch_content`` used to accept any
``http(s)`` URL with scheme-only validation, followed redirects blindly, and
the headless-browser sidecar could be pointed at the pod's own unauthenticated
CDP endpoint (``127.0.0.1:9222``). This module is the single gate in front of
every fetch/navigation the server performs.

Guarantees
----------
* **Scheme allowlist** — only ``http://`` and ``https://``.
* **Resolved-IP denylist** — the check is on the ADDRESSES the host resolves
  to (not just the name): loopback (``127.0.0.0/8``, ``::1``), unspecified
  (``0.0.0.0/8``, ``::``), RFC1918 private (``10/8``, ``172.16/12``,
  ``192.168/16``), link-local (``169.254/16`` — incl. the cloud-metadata
  endpoint ``169.254.169.254`` — and ``fe80::/10``), unique-local
  (``fc00::/7``), CGNAT/benchmark/multicast/reserved, and IPv4-mapped IPv6
  (``::ffff:127.0.0.1`` …) after unwrapping. Hostname forms of the same
  targets are blocked too: ``localhost``, cloud-metadata hostnames
  (``metadata.google.internal``, ``metadata``, …) and cluster-local suffixes
  (``*.svc``, ``*.svc.cluster.local``, ``*.cluster.local``).
* **Unconditional core** — loopback, unspecified, link-local/metadata
  targets are refused even when the allowlist below names them: the browser
  sidecar shares the pod network namespace, so navigating it at loopback
  would reach the MCP container and the CDP port, and the metadata address
  is unreachable-crown-jewel territory. This is the CDP route filter's
  foundation. Only RFC1918/ULA/cluster-internal names are escapable.
* **DNS pinning** — :func:`validate_fetch_url` resolves the host at
  check-time and returns a :class:`PinnedUrl` that connects to the
  *validated IP* (``sni_hostname``/``Host`` keep TLS + virtual-host routing
  intact), so check-time ≠ fetch-time DNS rebinding cannot reroute the
  request. Skipped when an HTTP(S) proxy is configured (the proxy then
  performs egress DNS; the denylist still ran on the check-time
  resolution).
* **Escapes** (decision D6 — guard is default-ON):
  * ``SEARXNG_FETCH_ALLOW_HOSTS`` — comma-separated hostnames (exact or
    ``.suffix``), IP literals, or CIDRs explicitly allowed *despite*
    resolving to private/cluster-internal space (legitimate internal
    targets). Additive, not authoritative: entries never bypass the
    unconditional core, and everything else keeps the standard policy.
  * ``SEARXNG_FETCH_DENY_EXTRA`` — comma-separated entries denied on top of
    the built-in list. Deny wins over allow.
* **Errors name the env** — every rejection message says which variable
  governs it so an operator can act (or allowlist) from the error alone.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "PinnedUrl",
    "UrlPolicyError",
    "is_pod_local_url",
    "preflight_fetch_url",
    "resolve_host_ips",
    "validate_fetch_url",
    "validate_fetch_url_async",
]

SCHEME_ALLOWLIST = ("http", "https")

# Addresses refused under EVERY configuration (allowlist cannot override).
# Loopback/unspecified: the pod itself — MCP server, SearXNG sidecar, and the
# unauthenticated CDP endpoint on 127.0.0.1:9222. Link-local: RFC3927
# metadata + the cloud-metadata service (169.254.169.254).
HARD_BLOCKED_CIDRS = (
    "0.0.0.0/8",  # "this host" / unspecified (IPv4)
    "127.0.0.0/8",  # IPv4 loopback
    "169.254.0.0/16",  # IPv4 link-local — cloud metadata lives here
    "::",  # IPv6 unspecified
    "::1",  # IPv6 loopback
    "fe80::/10",  # IPv6 link-local — metadata on v6 clouds
)

# Addresses refused by default but permitted when explicitly allowlisted
# (legitimate in-cluster targets: MinIO, internal wikis, Grafana, ...).
PRIVATE_BLOCKED_CIDRS = (
    "10.0.0.0/8",  # RFC1918
    "100.64.0.0/10",  # CGNAT shared space
    "172.16.0.0/12",  # RFC1918
    "192.168.0.0/16",  # RFC1918
    "192.0.2.0/24",  # TEST-NET-1 (documentation)
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "198.18.0.0/15",  # benchmarking
    "224.0.0.0/4",  # IPv4 multicast
    "240.0.0.0/4",  # IPv4 reserved
    "fc00::/7",  # IPv6 unique-local (pod/service nets)
    "2001:db8::/32",  # IPv6 documentation
    "ff00::/8",  # IPv6 multicast
)

# Hostnames refused under every configuration (the allowlist cannot unlock
# these; it exists for private *service* targets, not for the pod itself or
# the metadata service).
HARD_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        # cloud metadata endpoints (hostname forms)
        "metadata",
        "instance-data",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "metadata.oraclecloud.com",
    }
)

# Cluster-internal name suffixes — escapable via the allowlist.
CLUSTER_LOCAL_SUFFIXES = (
    ".svc",
    ".svc.cluster",
    ".svc.cluster.local",
    ".cluster.local",
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


class UrlPolicyError(ValueError):
    """A URL was rejected by the fetch SSRF policy.

    The message names the env var that governs the violated rule so the
    operator can act on it directly (``SEARXNG_FETCH_ALLOW_HOSTS`` /
    ``SEARXNG_FETCH_DENY_EXTRA``).
    """


@dataclass(frozen=True)
class PinnedUrl:
    """A validated URL plus everything needed to connect to the exact IP
    that was validated (DNS-rebinding pin).

    ``pin_active`` is False when an HTTP(S) proxy is configured: the proxy
    performs egress DNS, so rewriting the URL to the IP would break TLS SNI
    behind it; the request then goes out with the original hostname (the
    denylist still ran on the check-time resolution).
    """

    url: str  # the original, validated URL
    scheme: str
    host: str  # lowercase hostname (no brackets)
    port: int
    ip: str  # the validated address to connect to ("" when pin inactive)
    pinned_url: str  # URL with the host replaced by ``ip`` (= url when inactive)
    host_header: str  # original authority for the Host header ("" when inactive)
    sni_hostname: str  # TLS SNI / cert name ("" when inactive or http)
    pin_active: bool


# --------------------------------------------------------------------------
# env-driven configuration (read per call so tests can monkeypatch envs)
# --------------------------------------------------------------------------


def _split_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(e.strip() for e in raw.split(",") if e.strip())


def _proxy_configured() -> bool:
    """True when an HTTP(S) proxy env is set (fetch egress then goes through
    the proxy, which performs its own DNS — pinning is skipped)."""
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    )


# --------------------------------------------------------------------------
# matching helpers
# --------------------------------------------------------------------------


def _entry_matches(entry: str, host: str, ips: tuple[str, ...]) -> bool:
    """True when one allowlist/denylist entry matches the host (name, suffix,
    IP literal or CIDR) or any of its resolved addresses."""
    entry = entry.strip().lower().rstrip(".")
    if not entry:
        return False
    # CIDR (or bare IP) entry: match against resolved addresses
    try:
        net = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        net = None
    if net is not None:
        for ip_s in ips:
            try:
                addr = _unwrap(ipaddress.ip_address(ip_s))
            except ValueError:
                continue
            if addr in net:
                return True
        return False
    # hostname entry: exact or subdomain ("." prefix form included)
    if entry.startswith("."):
        return host == entry[1:] or host.endswith(entry)
    return host == entry or host.endswith("." + entry)


def _matches_any(host: str, ips: tuple[str, ...], entries: tuple[str, ...]) -> bool:
    return any(_entry_matches(e, host, ips) for e in entries)


def _unwrap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) before classification."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    return ip


def _in_any(ip, cidrs: tuple[str, ...]) -> bool:
    for c in cidrs:
        net = ipaddress.ip_network(c)
        if net.version == ip.version and ip in net:
            return True
    return False


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def resolve_host_ips(host: str) -> tuple[str, ...]:
    """Resolve *host* to a deduplicated tuple of IP literals.

    Raises ``OSError`` (``socket.gaierror``) when resolution fails — callers
    treat unresolved hosts as blocked.
    """
    infos = socket.getaddrinfo(host, None)
    ips: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr and addr not in ips:
            ips.append(addr)
    return tuple(ips)


def _resolve_for_validation(host: str) -> tuple[str, ...]:
    if host in HARD_BLOCKED_HOSTNAMES:
        # Don't even resolve; these names are refused on sight.
        return ()
    try:
        ipaddress.ip_address(host)
        return (host,)  # literal IP — nothing to resolve (no rebinding possible)
    except ValueError:
        pass
    try:
        return resolve_host_ips(host)
    except OSError as e:
        if any(host.endswith(s) for s in CLUSTER_LOCAL_SUFFIXES):
            # Cluster-internal name that this resolver can't answer: let the
            # name-based policy decide (blocked unless explicitly allowlisted)
            # instead of masking it as a generic resolution failure.
            return ()
        # Unresolved — treated as blocked (matches the reference policy: a
        # fetch to a name the resolver refuses has no legitimate place here).
        raise UrlPolicyError(f"blocked by the fetch SSRF policy: could not resolve host {host!r} ({e})") from e


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _reject(host: str, reason: str, env_hint: str | None = None) -> UrlPolicyError:
    msg = f"blocked by the fetch SSRF policy: {reason}"
    if env_hint:
        msg += f" ({env_hint})"
    return UrlPolicyError(msg)


def _hard_reason(host: str, ips: tuple[str, ...]) -> str | None:
    """Return a rejection reason when the target is in the unconditional
    (never-escapable) set, else None."""
    if host in HARD_BLOCKED_HOSTNAMES:
        if host == "localhost":
            return "loopback hostname 'localhost'"
        return f"cloud-metadata hostname '{host}'"
    for ip_s in ips:
        try:
            ip = _unwrap(ipaddress.ip_address(ip_s))
        except ValueError:
            continue
        if _in_any(ip, HARD_BLOCKED_CIDRS):
            if _in_any(ip, ("169.254.0.0/16", "fe80::/10")):
                label = "cloud-metadata/link-local address"
            else:
                label = "loopback address"
            return f"{label} {ip_s} (host {host!r})"
        # flag-based backstop for anything the explicit lists miss on this
        # interpreter (link-local/unspecified/multicast/reserved/loopback)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return f"non-routable address {ip_s} (host {host!r})"
    return None


def _private_reason(host: str, ips: tuple[str, ...]) -> str | None:
    """Return a rejection reason for the default-deny (allowlist-escapable)
    set, else None."""
    if any(host.endswith(s) for s in CLUSTER_LOCAL_SUFFIXES):
        return f"cluster-local hostname {host!r}"
    for ip_s in ips:
        try:
            ip = _unwrap(ipaddress.ip_address(ip_s))
        except ValueError:
            continue
        if _in_any(ip, PRIVATE_BLOCKED_CIDRS):
            return f"private/internal address {ip_s} (host {host!r})"
        if ip.is_private:  # backstop (e.g. newer special-purpose ranges)
            return f"private/internal address {ip_s} (host {host!r})"
    return None


def validate_fetch_url(url: str, *, for_browser: bool = False) -> PinnedUrl:
    """Validate *url* against the fetch SSRF policy and pin it to a
    validated address.

    Raises :class:`UrlPolicyError` on any violation; the message names the
    env var that governs the rule. ``for_browser`` only affects the wording
    (the headless-browser rung enforces the identical policy — loopback and
    metadata targets are unreachable from it in every configuration).
    """
    context = "browser navigation" if for_browser else "fetch"
    try:
        parts = urlsplit(str(url).strip())
    except ValueError as e:
        raise _reject("", f"malformed URL: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in SCHEME_ALLOWLIST:
        # keep the historical tool wording for every non-http(s) scheme
        raise UrlPolicyError("url must start with http:// or https://.")
    try:
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as e:  # malformed IPv6 literal etc.
        raise _reject("", f"malformed URL host: {e}") from e
    if not host:
        raise _reject("", f"{context} target has no host")
    try:
        port = parts.port
    except ValueError as e:
        raise _reject("", f"malformed URL port: {e}") from e
    port = port or _DEFAULT_PORTS[scheme]

    ips = _resolve_for_validation(host)

    # ---- unconditional core: loopback / metadata / unspecified / link-local
    hard = _hard_reason(host, ips)
    if hard:
        raise _reject(
            host,
            hard,
            "unreachable by policy; loopback, pod-local and metadata targets are never fetchable",
        )

    allowlist = _split_env("SEARXNG_FETCH_ALLOW_HOSTS")
    deny_extra = _split_env("SEARXNG_FETCH_DENY_EXTRA")

    # ---- explicit deny wins over everything, allowlist included
    if deny_extra and _matches_any(host, ips, deny_extra):
        raise _reject(host, f"host {host!r} is explicitly denied", "SEARXNG_FETCH_DENY_EXTRA")

    # ---- allowlist escape (D6): listed hosts may resolve privately
    allowlisted = bool(allowlist) and _matches_any(host, ips, allowlist)
    if not allowlisted:
        if allowlist:
            hint = f"add it to SEARXNG_FETCH_ALLOW_HOSTS to permit ({','.join(allowlist)})"
        else:
            hint = "set SEARXNG_FETCH_ALLOW_HOSTS to permit a legitimate internal target"
        private = _private_reason(host, ips)
        if private:
            raise _reject(host, private, hint)
        # allowlist set but this (public) host not matched -> standard policy
        # still applies, which for public hosts is: allowed. Nothing to do.
    # else: allowlisted — private classification intentionally ignored.

    # ---- DNS pinning: connect to the validated address
    if _proxy_configured():
        # A proxy performs egress DNS itself; rewriting the URL to the IP
        # would break TLS SNI behind it. Request the original hostname (the
        # denylist above still ran on the check-time resolution).
        return PinnedUrl(
            url=str(url).strip(),
            scheme=scheme,
            host=host,
            port=port,
            ip="",
            pinned_url=str(url).strip(),
            host_header="",
            sni_hostname="",
            pin_active=False,
        )
    ip = ips[0] if ips else host
    host_for_url = f"[{ip}]" if ":" in ip else ip
    raw_netloc = parts.netloc
    userinfo, at, hostport = raw_netloc.rpartition("@")
    if hostport.startswith("["):
        closing = hostport.find("]")
        rest = hostport[closing + 1 :] if closing != -1 else ""
        new_hostport = f"{host_for_url}{rest}"
    else:
        _, colon, p = hostport.partition(":")
        new_hostport = host_for_url + colon + p
    new_netloc = (userinfo + at + new_hostport) if at else new_hostport
    pinned_url = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    default_port = _DEFAULT_PORTS[scheme]
    host_header = host if port == default_port else f"{host}:{port}"
    sni = host if scheme == "https" else ""
    return PinnedUrl(
        url=str(url).strip(),
        scheme=scheme,
        host=host,
        port=port,
        ip=ip,
        pinned_url=pinned_url,
        host_header=host_header,
        sni_hostname=sni,
        pin_active=True,
    )


async def validate_fetch_url_async(url: str, *, for_browser: bool = False) -> PinnedUrl:
    """Async wrapper — DNS resolution (the only blocking part) runs in a
    worker thread so a slow resolver cannot stall the event loop."""
    return await asyncio.to_thread(validate_fetch_url, url, for_browser=for_browser)


def preflight_fetch_url(url: str) -> None:
    """DNS-free fast-fail gate for the tool entry point.

    Refuses everything resolution cannot influence — non-http(s) schemes,
    missing hosts, loopback/metadata hostnames, cluster-local suffixes and
    literal private/loopback/metadata IPs (allowlist respected, name/IP
    entries only) — before any rate budget, ladder, cache or browser work
    is spent. The full policy (resolved IPs, denylist, pin) is enforced
    per hop by :func:`validate_fetch_url` at fetch time either way; this
    preflight exists for fast, deterministic tool errors.
    """
    try:
        parts = urlsplit(str(url).strip())
    except ValueError as e:
        raise _reject("", f"malformed URL: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in SCHEME_ALLOWLIST:
        raise UrlPolicyError("url must start with http:// or https://.")
    try:
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as e:
        raise _reject("", f"malformed URL host: {e}") from e
    if not host:
        raise _reject("", "fetch target has no host")

    hard = _hard_reason(host, (host,) if _is_literal_ip(host) else ())
    if hard:
        raise _reject(
            host,
            hard,
            "unreachable by policy; loopback, pod-local and metadata targets are never fetchable",
        )

    allowlist = _split_env("SEARXNG_FETCH_ALLOW_HOSTS")
    deny_extra = _split_env("SEARXNG_FETCH_DENY_EXTRA")
    if deny_extra and any(_entry_matches(e, host, ()) for e in deny_extra):
        raise _reject(host, f"host {host!r} is explicitly denied", "SEARXNG_FETCH_DENY_EXTRA")
    if not any(_entry_matches(e, host, ()) for e in allowlist):
        private = _private_reason(host, (host,) if _is_literal_ip(host) else ())
        if private:
            hint = (
                f"add it to SEARXNG_FETCH_ALLOW_HOSTS to permit ({','.join(allowlist)})"
                if allowlist
                else "set SEARXNG_FETCH_ALLOW_HOSTS to permit a legitimate internal target"
            )
            raise _reject(host, private, hint)


def _is_literal_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_pod_local_url(url: str) -> bool:
    """Cheap, DNS-free check used by the browser route filter: True when the
    URL targets loopback / pod-local / cluster-local / metadata space by
    name or literal address. Every request the rendered page makes (main
    frame included) is aborted when this returns True."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return True
    if parts.scheme.lower() not in SCHEME_ALLOWLIST:
        return True
    try:
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host:
        return True
    if host in HARD_BLOCKED_HOSTNAMES:
        return True
    if any(host.endswith(s) for s in CLUSTER_LOCAL_SUFFIXES):
        return True
    try:
        ip = _unwrap(ipaddress.ip_address(host))
    except ValueError:
        return False  # a public-looking name — route-level checks stop here
    return _in_any(ip, HARD_BLOCKED_CIDRS) or _in_any(ip, PRIVATE_BLOCKED_CIDRS)
