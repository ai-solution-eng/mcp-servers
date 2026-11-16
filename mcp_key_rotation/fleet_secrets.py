#!/usr/bin/env python3
"""fleet_secrets - one YAML with every pre-deployment Secret the fleet needs.

Each chart references user-owned Secrets (existingSecret / credentialsSecret
knobs in values.yaml); the chart never creates them, so they must exist before
'helm install' or the pods sit in CreateContainerConfigError.

This script reads the values files per app, collects the Secret name and the
data key from those knobs, and emits ONE multi-document YAML:

    python3 fleet_secrets.py                 # rotate the API-key tier
    python3 fleet_secrets.py --from-cluster  # carry LIVE values; only rotated keys differ
    python3 fleet_secrets.py --all           # + gateway admin keys, postgres password
    python3 fleet_secrets.py --keep          # rotate nothing, just refresh

Values: entropy keys (api keys, passwords, tokens, signing keys) are generated
with the same shape as 'openssl rand -hex 32'.  Keys whose value must match
something that already exists (S3 access key, DB URL, upstream provider key,
tenant/client ids) are left as <FILL:...> for you to paste in.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets as _secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTRY = HERE / "registry.yaml"
OUTPUT = HERE / "secrets.fleet.yaml"

# Must match something that already exists -> never invent it.
EXTERNAL = re.compile(
    r"(ACCESS_KEY|ACCESSKEY|SECRET_KEY|TENANT|CLIENT_ID|CLIENTID|_URL$|_URI$|ENDPOINT|USERNAME|_USER$|^TOKEN$"
    r"|^MODEL_.*_API_KEY$)"
)
# Pure entropy the app itself validates -> mint it.
ENTROPY = re.compile(r"(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|API_KEYS|SALT|MASTER|_KEY$|-KEY$)", re.IGNORECASE)
FILL = "<FILL:"

# How a key reaches the app, which decides what rotating it costs.  The
# generator emits this next to every Secret so the file is self-explaining.
#   hot     mounted as a directory and re-read by the app -> rotation is live
#   restart env-injected -> kubelet reads it once at container start
#   never   rotating it destroys state the app cannot rebuild
ROTATION_HOT = "hot"
ROTATION_RESTART = "restart"
ROTATION_COORDINATED = "coordinated"
ROTATION_NEVER = "never"

#: Rotating the gateway's Fernet master key makes every stored endpoint
#: credential undecryptable (pcai_llm/core/secretbox.py:8 - by design).
NEVER_KEYS = frozenset({"master-key"})

#: Default classification per (app, secret name).  Overridable in registry.yaml
#: with a per-app 'rotation:' map.
ROTATION_DEFAULT: dict[tuple[str, str], str] = {
    ("MultimodalRAG", "rag-model-keys"): ROTATION_HOT,
    # The five MCP charts that mount the apiKey Secret at /etc/mcp-keys and
    # read <ENV>_FILE (mcp_auth.configured_keys) rotate live.
    ("searxng_mcp", "mcp-fleet-apikeys"): ROTATION_HOT,
    ("applygate_mcp", "mcp-fleet-apikeys"): ROTATION_HOT,
    ("logsearch_mcp", "mcp-fleet-apikeys"): ROTATION_HOT,
    ("workbench_mcp", "mcp-fleet-apikeys"): ROTATION_HOT,
    # k8s-mcp / ezapp-deploy charts were not wired (single-value key, no list)
    # and mm-rag's copy is consumed by the RAG app's own auth -> restart.
    ("PCAI_LLM", "pcai-llm-gateway-secrets"): ROTATION_NEVER,
    # The Postgres password must change IN THE DATABASE first (ALTER USER),
    # then here, then a gateway restart - reverse it and the gateway cannot
    # authenticate. Rotating is allowed; doing it blind is not.
    ("PCAI_LLM", "pcai-llm-gateway-pg-auth"): ROTATION_COORDINATED,
}


def rotation_class(app: str, secret: str, key: str, overrides: dict[str, str] | None = None) -> str:
    """hot | restart | never for one key."""
    if key in NEVER_KEYS:
        return ROTATION_NEVER
    if overrides and secret in overrides:
        return overrides[secret]
    return ROTATION_DEFAULT.get((app, secret), ROTATION_RESTART)


@dataclass
class SecretRef:
    namespace: str
    name: str
    keys: dict[str, list[str]] = field(default_factory=dict)  # key -> where it is consumed
    app: str = ""
    values_file: str = ""
    note: str = ""
    external: bool = False  # credentialsSecret: must match a system that already exists
    rotation: dict[str, str] = field(default_factory=dict)  # registry override: secret -> class


def deep_merge(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(out.get(k), v)
        return out
    return overlay if overlay is not None else base


def load_values(paths: list[Path]) -> dict:
    merged: dict = {}
    for path in paths:
        if path.exists():
            merged = deep_merge(merged, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return merged


def _key_siblings(node: dict, skip: str, prefix_only: bool) -> list[str]:
    """Data-key names carried by sibling knobs.

    prefix_only=True (an existingSecret block): only existingSecretKey /
    existingSecretApiKey / existingSecretMediaTokenKey qualify - a bare
    'secretKey' there is a VALUE seed, not a key name.
    prefix_only=False (a credentialsSecret block): every *Key sibling is a key
    name (accessKeyKey, secretKeyKey, tenantIdKey, tokenKey, ...).
    """
    keys: list[str] = []
    for k, v in node.items():
        if k == skip or not isinstance(k, str) or k == "create":
            continue
        if prefix_only and not k.startswith("existingSecret"):
            continue
        if not re.search(r"Key(s)?$", k):
            continue
        if isinstance(v, str) and v.strip():
            keys.append(v.strip())
        elif isinstance(v, list):
            keys.extend(str(x) for x in v if isinstance(x, str) and x.strip())
    return keys


def collect_refs(node: Any, app: str, ns: str, values_file: str, out: dict, parent: str = "") -> None:
    if isinstance(node, dict):
        name = node.get("existingSecret")
        if isinstance(name, str) and name.strip():
            ref = out.setdefault((ns, name.strip()), SecretRef(ns, name.strip(), app=app, values_file=values_file))
            for key in _key_siblings(node, "existingSecret", prefix_only=True):
                ref.keys.setdefault(key, []).append(f"{parent}.{key}")
        # list entries: {secretName: ..., secretKey: ...}
        if isinstance(node.get("secretName"), str) and node["secretName"].strip():
            sname = node["secretName"].strip()
            skey = node.get("secretKey")
            ref = out.setdefault((ns, sname), SecretRef(ns, sname, app=app, values_file=values_file))
            if isinstance(skey, str) and skey.strip():
                ref.keys.setdefault(skey.strip(), []).append(parent)
            else:
                ref.note = ref.note or "whole Secret (keys app-defined)"
        # credentialsSecret: {name: ..., xxxKey: ...}
        if parent.endswith("Secret") and isinstance(node.get("name"), str) and node["name"].strip():
            if node.get("create") is True:
                pass  # chart renders it from values - not a pre-deployment Secret
            else:
                sname = node["name"].strip()
                ref = out.setdefault(
                    (ns, sname), SecretRef(ns, sname, app=app, values_file=values_file, external=True)
                )
                for key in _key_siblings(node, "name", prefix_only=False):
                    ref.keys.setdefault(key, []).append(f"{parent}.{key}")
        for k, v in node.items():
            collect_refs(v, app, ns, values_file, out, f"{parent}.{k}" if parent else str(k))
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, app, ns, values_file, out, parent)


def _yaml_scalar(value: str) -> str:
    """Quote a scalar for the metadata blocks (labels/annotations)."""
    import json

    return json.dumps(value, ensure_ascii=False)


def prune_inactive(refs: dict, app_name: str, values: dict) -> None:
    """Drop Secrets the chart only reads in a deployment mode this app is not in.

    A credentialsSecret for a backend that is not selected is never consumed, so
    emitting it would create a Secret full of <FILL:...> literals - noise at
    best, a landmine if the mode ever flips.
    """
    backend = values.get("backend")
    embedded = bool(((values.get("database") or {}).get("embedded") or {}).get("enabled"))
    backend_sections = {"s3", "fabric", "iceberg"}
    drop: list[tuple[str, str]] = []
    for (ns, name), ref in refs.items():
        if ref.app != app_name:
            continue
        paths = [p for consumed in ref.keys.values() for p in consumed]
        tops = {p.split(".")[0] for p in paths}
        if backend and tops and tops <= backend_sections and backend not in tops:
            drop.append((ns, name))
        elif embedded and "database.database-url" in paths:
            drop.append((ns, name))
    for key in drop:
        refs.pop(key, None)
        print(f"  - {key[0]}/{key[1]}: not read in this deployment mode - not emitted", file=sys.stderr)


def mint(key: str, external: bool) -> str:
    """Mint an entropy value, or leave a <FILL:...> for something that must match.

    Matching is on the hyphen-normalised name: 'api-keys' has to read as
    API_KEYS, not as the literal 'api-keys' the values file happens to use.
    """
    normalised = key.upper().replace("-", "_")
    if external or EXTERNAL.search(normalised):
        return f"{FILL}{key}>"
    if ENTROPY.search(normalised):
        return _secrets.token_hex(32)
    return f"{FILL}{key}>"


def carry_existing_keys(refs: list[SecretRef], site_files: list[Path]) -> None:
    """Give key-less Secrets the data keys from a site's existing secrets file.

    Whole-Secret references (rag-model-keys is mounted, not read key by key)
    have app-defined keys, so values.yaml cannot name them - the site's own
    secrets.<site>.yaml can.
    """
    for ref in refs:
        if ref.keys:
            continue
        for site in site_files:
            candidate = site.with_name(site.name.replace("values", "secrets", 1))
            if not candidate.exists():
                continue
            for doc in yaml.safe_load_all(candidate.read_text(encoding="utf-8")):
                if not isinstance(doc, dict) or doc.get("kind") != "Secret":
                    continue
                if (doc.get("metadata") or {}).get("name") != ref.name:
                    continue
                data = doc.get("stringData") or doc.get("data") or {}
                for key in data:
                    ref.keys.setdefault(str(key), []).append(f"{candidate.name} (existing site secret)")
                ref.note = ref.note or f"keys carried from {candidate.name}"


def site_secret_values(paths: list[Path], namespace: str) -> dict[tuple[str, str, str], str]:
    """Real key material from a site's own secrets.<site>.yaml.

    Those files predate this generator and hold the LIVE values (model provider
    JWTs, S3 keys, endpoint overrides).  Without them the generator would mint
    replacements for credentials it cannot possibly invent - and applying that
    would break the deployment it was meant to help.
    """
    found: dict[tuple[str, str, str], str] = {}
    for path in paths:
        if not path.exists():
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict) or doc.get("kind") != "Secret":
                continue
            meta = doc.get("metadata") or {}
            name = str(meta.get("name") or "")
            ns = str(meta.get("namespace") or namespace)
            for key, value in (doc.get("stringData") or {}).items():
                if value is not None and str(value) != "":
                    found[(ns, name, str(key))] = str(value)
    return found


_KUBECTL_RESOLVED: list[str] | None = None


def _resolve_kubectl() -> list[str]:
    """Resolve the cluster CLI once: $KUBECTL, else 'k', else 'kubectl'.

    'k' is usually a FISH FUNCTION, not a binary - subprocess cannot execute a
    shell function, so when the name is not on PATH we run it through fish -c.
    A site whose alias lives only in an interactive config gets a clear error
    instead of a silent "every Secret is absent".
    """
    import shlex
    import shutil

    name = os.environ.get("KUBECTL") or "k"
    parts = shlex.split(name)
    if shutil.which(parts[0]):
        print(f"  using '{' '.join(parts)}'", file=sys.stderr)
        return parts
    fish = shutil.which("fish")
    if fish:
        for flags, label in ((["-c"], "fish -c"), (["-i", "-c"], "fish -i -c")):
            probe = subprocess.run(
                [fish, *flags, f"type -q {shlex.quote(parts[0])}"], capture_output=True, text=True
            )
            if probe.returncode == 0:
                print(f"  using '{' '.join(parts)}' ({label})", file=sys.stderr)
                return [fish, *flags, " ".join(shlex.quote(p) for p in parts)]
    fallback = shutil.which("kubectl")
    if fallback:
        print(
            f"  ! '{name}' is not executable here and no fish function matches; falling back to {fallback}",
            file=sys.stderr,
        )
        return [fallback]
    print(f"  ! cannot resolve '{name}' and no kubectl on PATH", file=sys.stderr)
    return parts


def run_kubectl(args: list[str]) -> subprocess.CompletedProcess:
    """Run the cluster CLI with *args* (works for a binary OR a fish function)."""
    global _KUBECTL_RESOLVED
    if _KUBECTL_RESOLVED is None:
        _KUBECTL_RESOLVED = _resolve_kubectl()
    import shlex

    base = _KUBECTL_RESOLVED
    if len(base) >= 2 and base[-2] == "-c":
        # base is [fish, "-c", "<cli>"] -> append args to the same command string
        cmdline = f"{base[-1]} {' '.join(shlex.quote(a) for a in args)}"
        return subprocess.run([*base[:-1], cmdline], capture_output=True, text=True, timeout=30, check=False)
    return subprocess.run([*base, *args], capture_output=True, text=True, timeout=30, check=False)


LIVE_META: dict[tuple[str, str], dict[str, dict[str, str]]] = {}


def live_values(refs: dict[tuple[str, str], "SecretRef"]) -> dict[tuple[str, str, str], str]:
    """Read the LIVE Secrets from the cluster, so the file matches reality.

    Without this the generator can only carry values it found on disk - a site
    secrets file written weeks ago, or a previous run of this tool.  Applying
    such a file silently reverts anything rotated in the cluster since.  With
    --from-cluster the only differences left in the manifest are the keys this
    run deliberately rotated, which makes 'kubectl diff' a real safety gate.
    """
    import base64
    import subprocess

    found: dict[tuple[str, str, str], str] = {}
    errors: list[str] = []
    for (ns, name) in sorted(refs):
        try:
            proc = run_kubectl(["get", "secret", "-n", ns, name, "-o", "yaml"])
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{ns}/{name}: {exc}")
            continue
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            msg = err[-1] if err else f"exit {proc.returncode}"
            if f'namespaces "{ns}" not found' in msg:
                # The NAMESPACE is missing: this Secret cannot be created there,
                # and a blind apply would fail. Never treat it as "absent".
                errors.append(f"{ns}/{name}: namespace {ns} does not exist")
            elif "NotFound" in msg:
                print(f"  - {ns}/{name}: absent in the cluster (will be created)", file=sys.stderr)
            else:
                errors.append(f"{ns}/{name}: {msg}")
            continue
        try:
            doc = yaml.safe_load(proc.stdout) or {}
        except yaml.YAMLError:
            continue
        meta = doc.get("metadata") or {}
        LIVE_META[(ns, name)] = {
            "labels": {k: str(v) for k, v in (meta.get("labels") or {}).items()},
            # helm.sh/resource-policy: keep is LOAD-BEARING - dropping it lets a
            # helm uninstall delete a Secret the chart never owned.
            "annotations": {
                k: str(v)
                for k, v in (meta.get("annotations") or {}).items()
                if not k.startswith("kubectl.kubernetes.io/")
            },
        }
        for block in ("stringData", "data"):
            for key, value in (doc.get(block) or {}).items():
                if block == "data":
                    try:
                        found[(ns, name, str(key))] = base64.b64decode(str(value)).decode("utf-8")
                    except Exception:  # noqa: BLE001 - opaque payload, skip it
                        continue
                elif value is not None:
                    found[(ns, name, str(key))] = str(value)
    if errors:
        print(
            f"\n  !! --from-cluster could not read {len(errors)} of {len(refs)} Secret(s). "
            "This is NOT 'they are absent' - the cluster view is incomplete, so the file would "
            "revert live values. Refusing to continue.",
            file=sys.stderr,
        )
        for item in errors[:12]:
            print(f"     {item}", file=sys.stderr)
        raise SystemExit(2)
    return found


def existing_metadata(path: Path) -> dict[tuple[str, str], dict[str, dict[str, str]]]:
    """labels/annotations from the manifest we are about to rewrite.

    Seeded into LIVE_META before any cluster read so a run WITHOUT --from-cluster
    (or a Secret that no longer exists in the cluster) cannot silently strip
    helm.sh/resource-policy: keep or the app label off a live object.
    """
    found: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    if not path.exists():
        return found
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        meta = doc.get("metadata") or {}
        ns, name = str(meta.get("namespace") or ""), str(meta.get("name") or "")
        if not name:
            continue
        found[(ns, name)] = {
            "labels": {k: str(v) for k, v in (meta.get("labels") or {}).items()},
            "annotations": {k: str(v) for k, v in (meta.get("annotations") or {}).items()},
        }
    return found


def existing_values(path: Path) -> dict[tuple[str, str, str], str]:
    """(namespace, name, key) -> value, from the manifest we are about to rewrite."""
    found: dict[tuple[str, str, str], str] = {}
    if not path.exists():
        return found
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        meta = doc.get("metadata") or {}
        ns = str(meta.get("namespace") or "")
        name = str(meta.get("name") or "")
        for key, value in (doc.get("stringData") or {}).items():
            found[(ns, name, str(key))] = "" if value is None else str(value)
    return found


ROTATION_NOTE = {
    ROTATION_HOT: "mounted as a directory and re-read by the app - rotating it is live, no restart",
    ROTATION_RESTART: "env-injected - rotating it needs a pod restart (kubectl rollout restart)",
    ROTATION_NEVER: "DO NOT ROTATE - rotating it destroys state the app cannot rebuild",
    ROTATION_COORDINATED: "must be changed in the backing system FIRST (see ROTATION.md), then here",
}


#: Apps this operator does not own: never rotated by the default tiers.
NOT_OWNED = frozenset({"ezapp-deploy"})


def is_api_key(key: str) -> bool:
    """The API-key tier: the per-app keys clients present to /mcp and /v1."""
    return key.lower() in ("api-keys", "api-key") or key.upper() == "RAG_API_KEY"


def render(
    refs: list[SecretRef],
    previous: dict[tuple[str, str, str], str],
    rotate: set[tuple[str, str, str]],
    rotate_master: bool = False,
) -> tuple[str, list[str], list[str]]:
    """Render the manifest; returns (document, warnings, rotated)."""
    warnings: list[str] = []
    rotated: list[str] = []
    lines = [
        "# " + "=" * 76,
        "# FLEET SECRETS - pre-deployment Secrets for every app - generated, do not hand-edit",
        "#",
        "#   python3 fleet_secrets/fleet_secrets.py            # rotate the API-key tier",
        "#   python3 fleet_secrets/fleet_secrets.py --all      # + gateway admin keys, postgres password",
        "#   python3 fleet_secrets/fleet_secrets.py --keep     # rotate nothing, just refresh",
        "#   kubectl apply -f fleet_secrets/secrets.fleet.yaml",
        "#",
        "# Entropy keys are already minted (openssl rand -hex 32 shape). <FILL:...>",
        "# marks a value that must match something that already exists - paste it in.",
        "#",
        "# Each Secret carries its rotation class: hot (live), restart (pod restart)",
        "# or never (destroys state). Procedures: fleet_secrets/ROTATION.md",
        "#",
        "# THIS FILE HOLDS LIVE KEY MATERIAL: mode 0600, gitignored, never committed.",
        "# " + "=" * 76,
        "",
    ]
    by_ns: dict[str, list[SecretRef]] = {}
    for ref in refs:
        by_ns.setdefault(ref.namespace, []).append(ref)
    for ns in sorted(by_ns):
        lines.append("")
        lines.append("# " + "-" * 76)
        lines.append(f"# namespace: {ns}")
        lines.append("# " + "-" * 76)
        for ref in sorted(by_ns[ns], key=lambda r: r.name):
            lines += [
                "---",
                f"# {ref.app} -> {ns}/{ref.name}",
                f"#   values: {ref.values_file}",
            ]
            if ref.external:
                lines.append("#   external credential: keys must MATCH the system they talk to (never invented)")
            if ref.note:
                lines.append(f"#   {ref.note}")
            classes = {
                rotation_class(ref.app, ref.name, key, ref.rotation): key for key in ref.keys
            }
            for key, where in sorted(ref.keys.items()):
                lines.append(f"#   key {key} <- {where[0]}")
            # One rotation line per Secret unless the keys genuinely differ: a
            # ten-key Secret repeating the same sentence ten times buries it.
            if len(classes) == 1:
                klass = next(iter(classes))
                lines.append(f"#   rotation: {klass} - {ROTATION_NOTE[klass]}")
            elif classes:
                for key in sorted(ref.keys):
                    klass = rotation_class(ref.app, ref.name, key, ref.rotation)
                    lines.append(f"#   rotation: {key} -> {klass} - {ROTATION_NOTE[klass]}")
            meta = LIVE_META.get((ns, ref.name)) or {}
            lines += [
                "apiVersion: v1",
                "kind: Secret",
                "metadata:",
                f"  name: {ref.name}",
                f"  namespace: {ns}",
            ]
            if meta.get("labels"):
                lines.append("  labels:")
                for k, v in sorted(meta["labels"].items()):
                    lines.append(f"    {k}: {_yaml_scalar(v)}")
            if meta.get("annotations"):
                lines.append("  annotations:")
                for k, v in sorted(meta["annotations"].items()):
                    lines.append(f"    {k}: {_yaml_scalar(v)}")
            lines.append("type: Opaque")
            if ref.keys:
                lines.append("stringData:")
                for key in sorted(ref.keys):
                    klass = rotation_class(ref.app, ref.name, key, ref.rotation)
                    kept = previous.get((ns, ref.name, key))
                    if klass == ROTATION_NEVER and not rotate_master:
                        # Protected: never re-mint, never silently change. A missing
                        # value is minted once (it has to exist) and flagged.
                        if kept and not kept.startswith(FILL):
                            value = kept
                        else:
                            value = mint(key, ref.external)
                            warnings.append(
                                f"{ns}/{ref.name} key {key} is protected (rotation={klass}) and had no value - "
                                "minted ONCE. Rotating it later destroys stored credentials; see ROTATION.md"
                            )
                    elif kept and (ns, ref.name, key) not in rotate and not kept.startswith(FILL):
                        value = kept  # only an explicit rotation tier re-mints
                    elif kept and klass == ROTATION_NEVER and not rotate_master:
                        value = kept
                    else:
                        # ONE KEY PER NAMESPACE: a Secret is namespace-scoped, so the
                        # copy in each namespace gets its own value.  A leak in one
                        # namespace cannot authenticate against the others.
                        value = mint(key, ref.external)
                        if kept and kept != value:
                            rotated.append(f"{ns}/{ref.name}/{key}")
                        if klass == ROTATION_COORDINATED and kept and kept != value:
                            warnings.append(
                                f"{ns}/{ref.name} key {key} was ROTATED - change it in the backing system FIRST "
                                "(e.g. ALTER USER for Postgres), then apply this file, then restart. "
                                "See fleet_secrets/ROTATION.md"
                            )
                    lines.append(f"  {key}: {value}")
            else:
                lines.append("stringData: {}")
    return "\n".join(lines) + "\n", warnings, rotated


MASTER_KEY_WARNING = """
=============================================================================
STOP - --rotate-master-keys rotates the PCAI LLM Gateway Fernet master key.

Rotating it makes EVERY stored endpoint credential undecryptable.  They are not
recoverable: pcai_llm/core/secretbox.py documents this as by-design (a single
Fernet key, SHA-256 derived, no key ring).  The gateway will not crash - each
call degrades to unauthenticated and the upstream 401s - but every upstream
credential must be re-saved from the admin panel afterwards.

Only do this for a suspected compromise.  Routine hygiene should rotate
admin-api-keys instead (comma-separated, overlap-friendly).

Re-run with --yes to confirm you understand the re-entry work.
=============================================================================
"""


def _pairs(refs: dict) -> set[tuple[str, str]]:
    return {(name, key) for (_ns, name), ref in refs.items() for key in ref.keys}


def cmd_naming(registry: dict) -> int:
    """Compare what the site deploys against what the chart defaults declare.

    'chart default' = render the app from values.yaml ALONE.  Where the site
    overlay renames a Secret or changes a data key, that is the divergence a
    customer-standard deployment would not have.
    """
    print(f"{'APP':<22} {'SOURCE':<8} SECRET / KEY")
    for app in registry.get("apps") or []:
        ns = str(app.get("namespace") or "")
        files = [ROOT / f for f in ([app.get("values", "values.yaml")] if isinstance(app.get("values"), str) else app.get("values") or ["values.yaml"])]
        site = app.get("site_values") or []
        site_files = [ROOT / s for s in ([site] if isinstance(site, str) else site)]
        base_refs: dict = {}
        collect_refs(load_values(files), app["name"], ns, "chart defaults", base_refs)
        site_refs: dict = {}
        collect_refs(load_values(files + site_files), app["name"], ns, "site", site_refs)
        for extra in app.get("extra_secrets") or []:
            site_refs.setdefault(
                (ns, extra["name"]),
                SecretRef(ns, extra["name"], app=app["name"], values_file="registry extra_secrets"),
            )
            for key in extra.get("keys") or []:
                site_refs[(ns, extra["name"])].keys.setdefault(str(key), []).append("extra")
        carry_existing_keys(list(site_refs.values()), site_files)
        base_pairs, site_pairs = _pairs(base_refs), _pairs(site_refs)
        if base_pairs == site_pairs:
            shown = ", ".join(f"{n}/{k}" for n, k in sorted(site_pairs)) or "(chart-managed only)"
            print(f"{app['name']:<22} {'match':<8} {shown}")
            continue
        for label, pairs in (("default", base_pairs), ("site", site_pairs)):
            if not pairs:
                print(f"{app['name']:<22} {label:<8} (none declared)")
                continue
            for name, key in sorted(pairs):
                print(f"{app['name']:<22} {label:<8} {name}/{key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--out", default=str(OUTPUT))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--keep", action="store_true", help="rotate NOTHING - just refresh the file in place")
    ap.add_argument(
        "--from-cluster",
        action="store_true",
        help="read LIVE Secret values via kubectl and carry them (makes the diff a safety gate)",
    )
    ap.add_argument("--kubectl", default=None, help="kubectl binary to use (default $KUBECTL or 'kubectl')")
    ap.add_argument("--all", action="store_true", help="also rotate gateway admin keys and the Postgres password")
    ap.add_argument("--gateway-admin", action="store_true", help="also rotate pcai-llm-gateway-admin/admin-api-keys")
    ap.add_argument("--postgres", action="store_true", help="also rotate pcai-llm-gateway-pg-auth/password")
    ap.add_argument(
        "--rotate-master-keys",
        action="store_true",
        help="also rotate protected master keys (destroys stored credentials; needs --yes)",
    )
    ap.add_argument("--yes", action="store_true", help="confirm a destructive rotation")
    ap.add_argument("--naming-report", action="store_true", help="show site vs chart-default secret naming")
    args = ap.parse_args(argv)

    if args.kubectl:
        os.environ["KUBECTL"] = args.kubectl
    registry = yaml.safe_load(Path(args.registry).read_text(encoding="utf-8")) or {}
    if args.naming_report:
        return cmd_naming(registry)

    if args.rotate_master_keys and not args.yes:
        print(MASTER_KEY_WARNING, file=sys.stderr)
        return 2

    refs: dict[tuple[str, str], SecretRef] = {}
    for app in registry.get("apps") or []:
        ns = str(app.get("namespace") or "")
        files = [ROOT / f for f in ([app.get("values", "values.yaml")] if isinstance(app.get("values"), str) else app.get("values") or ["values.yaml"])]
        site = app.get("site_values") or []
        files += [ROOT / s for s in ([site] if isinstance(site, str) else site)]
        values = load_values(files)
        label = ", ".join(str(f.relative_to(ROOT)) for f in files if f.exists())
        before = len(refs)
        collect_refs(values, app["name"], ns, label, refs)
        overrides = app.get("rotation") or {}
        for extra in app.get("extra_secrets") or []:
            ref = refs.setdefault(
                (ns, extra["name"]),
                SecretRef(ns, extra["name"], app=app["name"], values_file="registry extra_secrets"),
            )
            for key in extra.get("keys") or []:
                ref.keys.setdefault(str(key), []).append("registry extra_secrets")
        for ref in refs.values():
            if ref.app == app["name"]:
                ref.rotation = {str(k): str(v) for k, v in overrides.items()}
        carry_existing_keys(list(refs.values()), files)
        prune_inactive(refs, app["name"], values)
        found = len(refs) - before
        print(f"  {app['name']:<20} {ns:<18} {found} secret(s)", file=sys.stderr)

    # Which (app, secret, key) triples this run re-mints. Default = the API-key
    # tier only: the keys clients present to /mcp and /v1, which are cheap to
    # rotate and cheap to recover. Everything else needs a flag.
    rotate: set[tuple[str, str, str]] = set()
    if not args.keep:
        for ref in refs.values():
            if ref.app in NOT_OWNED:
                continue
            for key in ref.keys:
                tier_api = is_api_key(key)
                tier_gw = args.all or args.gateway_admin
                tier_pg = args.all or args.postgres
                if tier_api:
                    rotate.add((ref.namespace, ref.name, key))
                if tier_gw and key.lower() == "admin-api-keys":
                    rotate.add((ref.namespace, ref.name, key))
                if tier_pg and ref.name == "pcai-llm-gateway-pg-auth":
                    rotate.add((ref.namespace, ref.name, key))

    # Value precedence. The site's own secrets.<site>.yaml is the authority for
    # everything this run does NOT rotate - it holds the live provider keys, S3
    # credentials and endpoint overrides, which cannot be invented. The previous
    # fleet file wins only for keys the site does not carry, or when --keep says
    # "touch nothing".
    previous = existing_values(Path(args.out))
    site_vals: dict[tuple[str, str, str], str] = {}
    for app in registry.get("apps") or []:
        ns = str(app.get("namespace") or "")
        site = app.get("site_values") or []
        for entry in [site] if isinstance(site, str) else site:
            site_path = ROOT / entry
            candidates = [
                site_path.with_name(site_path.name.replace("values", "secrets", 1)),
                site_path.parent / "secrets.yaml",
            ]
            site_vals.update(site_secret_values(candidates, ns))
    # Metadata: previous manifest first, live cluster overrides. A run without
    # --from-cluster must not strip labels/annotations off objects it re-emits.
    LIVE_META.clear()
    LIVE_META.update(existing_metadata(Path(args.out)))

    overlay: dict[tuple[str, str, str], str] = {}
    if args.from_cluster:
        overlay.update(live_values(refs))
    overlay.update({k: v for k, v in site_vals.items() if k not in overlay})
    if args.keep:
        for triple, value in previous.items():
            overlay.setdefault(triple, value)
    else:
        for triple, value in previous.items():
            overlay.setdefault(triple, value)

    document, warnings, rotated = render(
        list(refs.values()), overlay, rotate, args.rotate_master_keys
    )
    if args.stdout:
        sys.stdout.write(document)
    else:
        Path(args.out).write_text(document, encoding="utf-8")
        os.chmod(args.out, 0o600)
        print(f"\n{args.out}: {len(refs)} Secret(s), mode 0600", file=sys.stderr)
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)
    if rotated:
        print(f"\nRotated {len(rotated)} key(s) - apply, then restart the consumers:", file=sys.stderr)
        for item in rotated:
            print(f"  ~ {item}", file=sys.stderr)
    else:
        print("\nNo keys rotated (--keep, or nothing in the selected tier).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
