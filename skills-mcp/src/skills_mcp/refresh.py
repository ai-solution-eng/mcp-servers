"""Refresh the skills catalogue from the tools repo — replace, never merge.

Three layers, one rule (git wins, the pod is a cache):

  bundled snapshot (image)  ->  seeds the working copy when it is empty
  working copy (/data)      ->  what the tools read, via the skills-current symlink
  GitHub tarball            ->  REPLACES the working copy, on demand or on a timer

A refresh is: download -> extract ONLY the source paths -> parse every SKILL.md
-> if zero skills parsed, ABORT -> write a new release dir -> swap the symlink
in ONE os.replace() -> prune old releases. A failure at any step leaves the
previous release serving: there is no half-updated catalogue.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import catalog as catalog_mod
from . import state as state_mod
from .state import Source, State, bundled_root, load_state, source_paths

ENV_SOURCE_URL = "SKILLS_SOURCE_URL"
ENV_SOURCE_REF = "SKILLS_SOURCE_REF"
ENV_REFRESH_INTERVAL = "SKILLS_REFRESH_INTERVAL_S"
ENV_ROOT = "SKILLS_ROOT"

DEFAULT_SOURCE_REF = "main"
DEFAULT_REFRESH_INTERVAL_S = 14400  # 4 h
DEFAULT_ROOT = "/data"
KEEP_RELEASES = 3
DOWNLOAD_TIMEOUT_S = 30
MAX_TARBALL_BYTES = 64 * 1024 * 1024
STATE_FILE = "state.json"
CURRENT_LINK = "skills-current"

# Refresh runs in a worker thread (asyncio.to_thread) and must never overlap:
# two concurrent swaps would race on the symlink and the prune.
_LOCK = threading.Lock()
_LAST_ERROR: str | None = None


class RefreshError(RuntimeError):
    """A refresh that failed. The previous release keeps serving."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def source_url() -> str:
    return os.getenv(ENV_SOURCE_URL, "").strip()


def source_ref() -> str:
    return os.getenv(ENV_SOURCE_REF, "").strip() or DEFAULT_SOURCE_REF


def refresh_interval_s() -> int:
    raw = os.getenv(ENV_REFRESH_INTERVAL, "").strip()
    try:
        return int(raw) if raw else DEFAULT_REFRESH_INTERVAL_S
    except ValueError:
        return DEFAULT_REFRESH_INTERVAL_S


def refresh_enabled() -> bool:
    """A refresh needs a source URL. Unset = bundled snapshot only."""
    return bool(source_url())


def root_dir() -> Path:
    return Path(os.getenv(ENV_ROOT, "").strip() or DEFAULT_ROOT)


def last_error() -> str | None:
    return _LAST_ERROR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Boot: seed the working copy
# ---------------------------------------------------------------------------


def ensure_working_copy() -> State | None:
    """Return the state for the current working copy, seeding it from the
    bundled snapshot when it is missing or unreadable. None = no source at all."""
    root = root_dir()
    link = root / CURRENT_LINK

    if link.is_symlink() or link.exists():
        target = link.resolve()
        if target.is_dir():
            meta = _read_state_file(target)
            candidate = load_state(
                target,
                Source(
                    origin=str(meta.get("origin") or "github"),
                    ref=meta.get("ref"),
                    commit=meta.get("commit"),
                    refreshed_at=meta.get("refreshed_at"),
                ),
            )
            if len(candidate.catalog) > 0:
                return candidate
            # A working copy that parses to zero skills is broken: fall through
            # and re-seed from the image rather than serve an empty catalogue.

    bundled = bundled_root()
    if not bundled.is_dir():
        return None

    # Point the working copy at the bundled snapshot. If /data is not writable
    # (a read-only root filesystem), serving straight from the bundle is fine.
    try:
        root.mkdir(parents=True, exist_ok=True)
        _swap_link(root, bundled)
    except OSError:
        pass
    return load_state(bundled, Source(origin="bundled", ref=_read_bundled_ref(bundled)))


def _read_bundled_ref(bundled: Path) -> str | None:
    try:
        return (bundled / ".ref").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def refresh_from_source() -> dict:
    """Fetch, validate, swap. Returns the success payload; raises RefreshError."""
    global _LAST_ERROR

    url = source_url()
    if not url:
        raise RefreshError("refresh disabled: SKILLS_SOURCE_URL is not set")

    with _LOCK:
        root = root_dir()
        ref = source_ref()
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".refresh-", dir=root) as tmp:
                tmp_path = Path(tmp)
                tarball = tmp_path / "source.tar.gz"
                _download(url.rstrip("/") + "/" + ref, tarball)

                release = tmp_path / "release"
                release.mkdir()
                _extract(tarball, release, source_paths())

                catalog = catalog_mod.load_catalog(release, source_paths())
                if len(catalog) == 0:
                    raise RefreshError(
                        "refusing to swap: 0 skills parsed from the fetched tarball "
                        "(wrong repo path or bad ref?)"
                    )

                source = Source(
                    origin="github",
                    ref=ref,
                    commit=_resolve_commit(url, ref),
                    refreshed_at=_now_iso(),
                )
                _write_state_file(release, source)

                label = (source.commit or "")[:12] or datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
                releases = root / "releases"
                releases.mkdir(exist_ok=True)
                final = releases / label
                if final.exists():
                    shutil.rmtree(final)
                shutil.move(str(release), str(final))

                # THE swap: one atomic os.replace of the symlink. Readers either
                # see the old release or the new one, never a half-written tree.
                _swap_link(root, final)
                served = load_state(final, source)
                state_mod.set_state(served)
                _prune(releases, current=final)
                _LAST_ERROR = None

                return {
                    "ok": True,
                    "origin": source.origin,
                    "ref": source.ref,
                    "commit": source.commit,
                    "count": len(served.catalog),
                    "warnings": served.catalog.warnings,
                    "refreshed_at": source.refreshed_at,
                }
        except RefreshError as exc:
            _LAST_ERROR = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - one error shape for the caller
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
            raise RefreshError(_LAST_ERROR) from exc


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "skills-mcp (refresh)",
            "Accept": "application/gzip, application/octet-stream, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            status = getattr(response, "status", None)
            if status not in (None, 200):
                raise RefreshError(f"fetch failed: HTTP {status} from {url}")
            total = 0
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_TARBALL_BYTES:
                        raise RefreshError(
                            f"fetch aborted: tarball exceeds {MAX_TARBALL_BYTES} bytes"
                        )
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RefreshError(f"fetch failed: HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RefreshError(f"fetch failed: {exc.reason} ({url})") from exc


def _extract(tar_path: Path, dest: Path, sources: list[str]) -> None:
    """Extract ONLY <top>/<source path>/... members, safely.

    Members are written one by one: no symlinks, no devices, no absolute paths
    and no '..' — a tarball is untrusted input even when it comes from GitHub.
    """
    with tarfile.open(tar_path, "r:gz") as archive:
        members = [m for m in archive.getmembers() if m.name]
        tops = {m.name.split("/", 1)[0] for m in members}
        tops.discard("")
        if len(tops) != 1:
            raise RefreshError(f"unexpected tarball layout: {len(tops)} top-level entries")
        top = tops.pop()

        wanted = []
        for member in members:
            parts = member.name.split("/")
            if len(parts) >= 2 and parts[0] == top and parts[1] in sources:
                wanted.append(member)
        if not wanted:
            raise RefreshError(
                "tarball contains none of the configured source paths: " + ",".join(sources)
            )

        for member in wanted:
            name = PurePosixPath(member.name)
            if name.is_absolute() or any(part == ".." for part in name.parts):
                raise RefreshError(f"refusing unsafe tarball member: {member.name}")
            target = dest.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue  # symlinks/devices are never needed for skills
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)

    # Lift <top>/<source> to <dest>/<source> so the release root looks exactly
    # like a checkout root: skills/ and (optionally) skills-demo/.
    extracted_top = dest / top
    for name in sources:
        candidate = extracted_top / name
        if candidate.is_dir():
            shutil.move(str(candidate), str(dest / name))
    shutil.rmtree(extracted_top, ignore_errors=True)


_CODELOAD_RE = re.compile(
    r"^https?://(?:www\.)?codeload\.github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tar\.gz/?$"
)


def _resolve_commit(url: str, ref: str) -> str | None:
    """Best-effort commit sha for the fetched ref. None is fine (rate limit,
    no network, a non-GitHub URL) — the refresh itself already succeeded."""
    match = _CODELOAD_RE.match(url)
    if not match:
        return None
    api = f"https://api.github.com/repos/{match.group('owner')}/{match.group('repo')}/commits/{ref}"
    try:
        request = urllib.request.Request(
            api,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "skills-mcp (refresh)"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        sha = payload.get("sha")
        return sha if isinstance(sha, str) else None
    except Exception:  # noqa: BLE001 - best effort by design
        return None


# ---------------------------------------------------------------------------
# Working-copy plumbing
# ---------------------------------------------------------------------------


def _swap_link(root: Path, target: Path) -> None:
    """Point <root>/skills-current at <target> atomically."""
    link = root / CURRENT_LINK
    temporary = root / (".swap-" + str(os.getpid()) + "-" + str(threading.get_ident()))
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(str(target), str(temporary))
    os.replace(str(temporary), str(link))


def _write_state_file(release: Path, source: Source) -> None:
    payload = {
        "origin": source.origin,
        "ref": source.ref,
        "commit": source.commit,
        "refreshed_at": source.refreshed_at,
    }
    temporary = release / (STATE_FILE + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, release / STATE_FILE)


def _read_state_file(release: Path) -> dict:
    try:
        payload = json.loads((release / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _prune(releases: Path, current: Path) -> None:
    """Keep the newest KEEP_RELEASES release dirs (the current one always)."""
    entries = [
        entry
        for entry in releases.iterdir()
        if entry.is_dir() and entry.resolve() != current.resolve()
    ]
    entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for stale in entries[KEEP_RELEASES - 1 :]:
        shutil.rmtree(stale, ignore_errors=True)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_payload() -> dict:
    served = state_mod.current_state()
    return {
        "origin": served.source.origin,
        "ref": served.source.ref,
        "commit": served.source.commit,
        "refreshed_at": served.source.refreshed_at,
        "count": len(served.catalog),
        "warnings": served.catalog.warnings,
        "source_dir": str(served.directory) if served.directory else None,
        "refresh": {
            "enabled": refresh_enabled(),
            "url": source_url() or None,
            "ref": source_ref(),
            "interval_s": refresh_interval_s(),
            "last_error": _LAST_ERROR,
        },
    }
