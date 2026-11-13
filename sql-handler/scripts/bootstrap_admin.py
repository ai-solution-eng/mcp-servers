#!/usr/bin/env python3
"""Bootstrap a new SQLhandler deployment's administration plane in one step.

WHY THIS EXISTS
---------------
A SQLhandler deployment has TWO independent objects, and creating one does not
create the other:

* Secret "sqlhandler-apikey" (key "api-keys") -> MCP_API_KEYS
  -> AUTHENTICATION. The chart wires it unconditionally; the pod sits in
  CreateContainerConfigError until it exists.
* ConfigMap "sqlhandler-policy" (key "policy.json") -> the ACL document,
  including "admins" -> AUTHORIZATION. The chart only SEEDS a fresh claim
  from it; after first boot the live file on the PVC is the truth.

The link between them is a FINGERPRINT ("sha256:" + the first 12 hex chars of
sha256(key)), written into "admins". Nothing derives it automatically, so a
hand-computed hash that disagrees with the server locks the panel out: the old
key 401s (not configured) and the new one 403s (not designated).

This script computes the fingerprint with the SERVER'S OWN function
(sqlhandler.mcp_fleet_common.audit.key_fingerprint), emits both objects from
ONE generated key, and can apply them with "kubectl apply -f -".

Discipline (same as mint_key.py): stdlib only, no network, no filesystem
writes. The raw key goes to STDOUT only.

Usage
-----
    python scripts/bootstrap_admin.py                      # print commands
    python scripts/bootstrap_admin.py --apply              # apply with kubectl
    python scripts/bootstrap_admin.py --subject andrew-bydlon
    python scripts/bootstrap_admin.py --key "$EXISTING"    # reuse a key
    python scripts/bootstrap_admin.py --namespace other --secret other-apikey

Then deploy with:
    security.policy.enabled: true
    security.policy.existingConfigMap: sqlhandler-policy
    security.apiKey.existingSecret: sqlhandler-apikey

A policy with ONLY an "admins" list is legal: no groups means no ACL
enforcement, just a working administration plane. Author restrictions from
the panel afterwards.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path


def _bootstrap_src() -> Path:
    """Locate the repo's src/ so the fingerprint comes from audit.py itself.

    A locally-reimplemented hash could silently drift from the server's, and a
    fingerprint that disagrees with the auth layer is worse than no key.
    """
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "src" / "sqlhandler" / "mcp_fleet_common" / "audit.py"
        if candidate.is_file():
            return base / "src"
    print(
        "bootstrap_admin: could not locate src/sqlhandler/mcp_fleet_common/audit.py "
        f"above {here.parent} - run the script from inside the SQLhandler repo.",
        file=sys.stderr,
    )
    raise SystemExit(1)


sys.path.insert(0, str(_bootstrap_src()))

from sqlhandler.mcp_fleet_common.audit import key_fingerprint  # noqa: E402

DEFAULT_NAMESPACE = "sqlhandler"
DEFAULT_SECRET = "sqlhandler-apikey"
DEFAULT_SECRET_KEY = "api-keys"
DEFAULT_CONFIGMAP = "sqlhandler-policy"
DEFAULT_POLICY_KEY = "policy.json"


def build_policy(admins: list[str]) -> str:
    """The minimal bootstrap policy: an "admins" list and nothing else.

    No groups = no ACL enforcement (the permissive default), which is the
    correct first-install state: the operator authors restrictions from the
    panel once they can reach it.
    """
    return json.dumps({"admins": admins}, indent=2) + "\n"


def build_manifests(
    *,
    namespace: str,
    secret_name: str,
    secret_key: str,
    key: str,
    configmap_name: str,
    policy_key: str,
    policy_json: str,
) -> str:
    """Both bootstrap objects as one multi-document YAML (kubectl apply -f -)."""
    indented = "".join(f"    {line}\n" for line in policy_json.rstrip("\n").split("\n"))
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {secret_name}\n"
        f"  namespace: {namespace}\n"
        "type: Opaque\n"
        "stringData:\n"
        f"  {secret_key}: {key}\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: {configmap_name}\n"
        f"  namespace: {namespace}\n"
        "data:\n"
        f"  {policy_key}: |\n"
        f"{indented}"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap_admin.py",
        description="Create the /mcp key Secret AND the policy ConfigMap designating it admin.",
        epilog=(
            "The two objects are independent: the Secret authenticates, the "
            "policy authorizes. This script derives the fingerprint from the "
            "same key with the server's own function so they cannot disagree."
        ),
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="the /mcp key Secret name")
    parser.add_argument("--secret-key", default=DEFAULT_SECRET_KEY, help="its data key")
    parser.add_argument("--configmap", default=DEFAULT_CONFIGMAP, help="the policy ConfigMap name")
    parser.add_argument("--policy-key", default=DEFAULT_POLICY_KEY, help="its data key")
    parser.add_argument(
        "--subject",
        action="append",
        default=[],
        metavar="NAME",
        help="also designate an SSO subject (repeatable); survives key rotation",
    )
    parser.add_argument("--key", default="", metavar="HEX", help="reuse an existing key instead of generating one")
    parser.add_argument("--apply", action="store_true", help="run kubectl apply -f - instead of printing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    key = args.key.strip() or secrets.token_hex(32)
    fp = key_fingerprint(key)
    admins = [f"key:{fp}"]
    admins.extend(f"subject:{name.strip()}" for name in args.subject if name.strip())
    policy_json = build_policy(admins)
    manifests = build_manifests(
        namespace=args.namespace,
        secret_name=args.secret,
        secret_key=args.secret_key,
        key=key,
        configmap_name=args.configmap,
        policy_key=args.policy_key,
        policy_json=policy_json,
    )

    print(f"key         : {key}")
    print(f"fingerprint : {fp}")
    print(f"admins      : {json.dumps(admins)}")
    print()
    print("# policy document")
    print(policy_json, end="")
    print()
    print("# objects (Secret authenticates; ConfigMap authorizes)")
    print(manifests, end="")

    if args.apply:
        print(f"# applying to namespace {args.namespace} ...", file=sys.stderr)
        try:
            subprocess.run(
                ["kubectl", "-n", args.namespace, "apply", "-f", "-"],
                input=manifests,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            print("bootstrap_admin: kubectl not found on PATH", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as exc:
            print(f"bootstrap_admin: kubectl apply failed (exit {exc.returncode})", file=sys.stderr)
            return exc.returncode
    else:
        print()
        print("# apply with:")
        print(f"#   python {Path(__file__).name} --apply   (or pipe the YAML above)")
    print()
    print("# deploy with: security.policy.enabled=true, security.policy.existingConfigMap=" + args.configmap)
    print("#              security.apiKey.existingSecret=" + args.secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
