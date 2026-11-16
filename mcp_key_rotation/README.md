# fleet_secrets

One YAML with every pre-deployment Secret the fleet needs — MCP servers, RAG,
SQL, and the PCAI LLM Gateway. The charts never create these themselves (they
reference them through `existingSecret` / `credentialsSecret` knobs), so they
must exist before `helm install` or the pods sit in `CreateContainerConfigError`.

```bash
python3 fleet_secrets/fleet_secrets.py      # write secrets.fleet.yaml (0600)
python3 fleet_secrets/verify.py             # prove every chart ref is covered
kubectl apply -f fleet_secrets/secrets.fleet.yaml
```

Rotating any of them: **[ROTATION.md](ROTATION.md)** — it records, per Secret,
whether rotation is live or needs a restart, and which one must never be rotated.

## What it reads

`registry.yaml` lists each app: chart, namespace, and the values files to
merge (defaults + the site overlay). The Secret **name** and **data key** come
from those files:

| values shape | example | becomes |
|---|---|---|
| `x.existingSecret` + `x.existingSecretKey` | `apiKey.existingSecret: mcp-fleet-apikeys` | Secret `mcp-fleet-apikeys`, key `api-keys` |
| `x.existingSecretApiKey` / `existingSecretMediaTokenKey` | `security.existingSecretApiKey: RAG_API_KEY` | extra keys on the same Secret |
| `credentialsSecret.name` + `*Key` siblings | `s3.credentialsSecret: {name: s3-credentials, accessKeyKey: access-key}` | Secret `s3-credentials`, key `access-key` |
| `{secretName, secretKey}` list entries | `upstreamKeySecrets[]` | one Secret per entry |

A chart-rendered Secret (`create: true`) is skipped on purpose: helm owns it,
and pre-creating it collides on ownership metadata.

## Values

- **Minted** — entropy the app itself validates (API keys, passwords, tokens,
  master/signing keys): `openssl rand -hex 32` shape, 64 hex chars.
- **`<FILL:...>`** — anything that must match something that already exists:
  S3/MinIO access keys, DB URLs, tenant/client ids, upstream provider keys,
  endpoint overrides.
- **One key per namespace.** A Secret is namespace-scoped, so `mcp-fleet-apikeys`
  carries a DIFFERENT value in each of the five namespaces it exists in: a leak
  in one namespace cannot authenticate against the others. Clients therefore hold
  one key per server, not one fleet-wide key.

## Rotation safety

Every key is classified in the generated file:

| class | meaning | applies to |
|---|---|---|
| `hot` | mounted as a directory, re-read by the app | RAG `rag-model-keys`; MCP `*_API_KEYS` |
| `restart` | env-injected, read once at container start | everything else |
| `coordinated` | rotate only in lock-step with the backing system | Postgres `password` |
| `never` | rotating destroys state the app cannot rebuild | PCAI LLM Gateway `master-key` |

The default invocation rotates the **API-key tier only** — the keys clients
present to `/mcp` and `/v1`. Everything more invasive needs a flag:

| command | rotates |
|---|---|
| `fleet_secrets.py` | API keys: `mcp-fleet-apikeys` (5 ns), `k8s-mcp-2-0-apikey`, `sqlhandler-apikey`, `RAG_API_KEY` |
| `--gateway-admin` | + `pcai-llm-gateway-admin/admin-api-keys` |
| `--postgres` | + `pcai-llm-gateway-pg-auth/password` (warns: ALTER USER first) |
| `--all` | both of the above |
| `--keep` | nothing — refresh the file in place |
| `--rotate-master-keys --yes` | the gateway master key (destructive) |

`ezapp-deploy` is excluded from every tier (`NOT_OWNED`) — this operator does
not own that deployment.

`--rotate-master-keys` opts into rotating those too, and refuses to run without
`--yes`:

```bash
python3 fleet_secrets/fleet_secrets.py --rotate-master-keys
#   -> prints the consequence (every stored endpoint credential becomes
#      undecryptable and must be re-saved) and exits 2
python3 fleet_secrets/fleet_secrets.py --rotate-master-keys --yes
#   -> actually rotates
```

## Cluster CLI

`--from-cluster` shells out to the cluster. The default command is `k` (this
site's fish function); override with `--kubectl <cmd>` or `$KUBECTL`.

`k` is usually a fish *function*, which `subprocess` cannot execute directly, so
the resolver tries in order and prints which one it used:

1. `k` as a binary on `PATH`
2. `fish -c` — autoloaded functions (`~/.config/fish/functions/k.fish`)
3. `fish -i -c` — functions defined only in an interactive config
4. `kubectl` — with a warning, so a wrong context never passes silently

If none of them can read a Secret, `--from-cluster` **exits 2 without writing the
file**: an incomplete cluster view would otherwise produce a manifest that
reverts live values.

## Naming consistency

`--naming-report` compares what the site deploys against what the chart
defaults declare, so a deployment can be brought back to customer-standard
naming:

```
$ python3 fleet_secrets/fleet_secrets.py --naming-report
APP                    SOURCE   SECRET / KEY
logsearch_mcp          default  logsearch-mcp-apikey/api-keys
logsearch_mcp          site     mcp-fleet-apikeys/api-keys
```

The MCP servers are the only real divergence: the site shares one
`mcp-fleet-apikeys` across five namespaces (which the gateway references via
`mcpKeySecrets`), while each chart's default is its own `<name>-apikey`.
Switching to defaults means per-app secrets and a gateway values change — a
deliberate choice, not a typo.

## Files

| file | role |
|---|---|
| `fleet_secrets.py` | the generator |
| `registry.yaml` | the tracking table: app → chart, namespace, values files |
| `secrets.fleet.yaml` | generated output — 0600, gitignored, never committed |
| `verify.py` | renders each chart and diffs its Secret refs against the output |
| `ROTATION.md` | per-secret rotation runbook |

## Adding an app

Add a registry entry, run the generator, run `verify.py`. If a chart falls back
to a name that values.yaml never spells out (e.g. `<deployment.name>-apikey`),
declare it under `extra_secrets` — that is how `ezapp-deploy` is handled.
