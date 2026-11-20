# mcp_key_rotation

Generates the pre-deployment Kubernetes Secrets your PCAI apps need, and rotates
them with a diff you can inspect before anything reaches the cluster.

The charts in this fleet never create their own credentials — they reference
user-owned Secrets through `existingSecret` / `credentialsSecret` knobs. So the
Secrets have to exist before `helm install`, or the pods sit in
`CreateContainerConfigError` with nothing obviously wrong.

This tool derives those Secrets from your values files, emits them as one
manifest, and keeps rotation auditable.

## Getting started

Everything runs **from this directory**.

```bash
cp registry.yaml registry.yaml.orig     # keep the example for reference
$EDITOR registry.yaml                   # point it at YOUR charts
python3 fleet_secrets.py --keep         # generate secrets.fleet.yaml (0600)
kubectl apply -f secrets.fleet.yaml
```

`registry.yaml` ships as a **template**: its paths (`mcp_servers/<name>/helm/...`)
describe one reference deployment. Replace them with your chart locations and
namespaces before the first run. The generator reads your values files from
there and nothing else.

Paths in `registry.yaml` resolve relative to the **parent of this directory**, not
the working directory of your shell. Either place this directory at the root of your
chart tree:

```
your-repo/
|-- mcp_key_rotation/     <- this tool
|-- mcp_servers/          <- registry paths start here
`-- helm/...
```

or use absolute paths in `registry.yaml`.

## Daily use

```bash
python3 fleet_secrets.py --from-cluster   # rotate, carrying live values
kubectl diff -f secrets.fleet.yaml        # THE GATE - read it
kubectl apply -f secrets.fleet.yaml
```

The diff is the safety mechanism. After a `--from-cluster` run it should show
only the keys you asked to rotate; anything else means something would be
reverted, and you stop.

```bash
python3 verify.py                         # every chart Secret ref covered?
```

## What it reads

`registry.yaml` lists each app: chart, namespace, and the values files to merge
(defaults + site overlay). The Secret **name** and **data key** come from those
files:

| values shape | example | becomes |
|---|---|---|
| `x.existingSecret` + `x.existingSecretKey` | `apiKey.existingSecret: searxng-mcp-apikey` | Secret `searxng-mcp-apikey`, key `api-keys` |
| `x.existingSecretApiKey` / `existingSecretMediaTokenKey` | `security.existingSecretApiKey: RAG_API_KEY` | extra keys on the same Secret |
| `credentialsSecret.name` + `*Key` siblings | `s3.credentialsSecret: {name: s3-credentials, accessKeyKey: access-key}` | Secret `s3-credentials`, key `access-key` |
| `{secretName, secretKey}` list entries | `upstreamKeySecrets[]` | one Secret per entry |

A chart-rendered Secret (`create: true`) is skipped on purpose: helm owns it, and
pre-creating it collides on ownership metadata.

## Values

- **Minted** — entropy the app itself validates (API keys, passwords, tokens,
  master/signing keys): `openssl rand -hex 32` shape, 64 hex characters.
- **`<FILL:...>`** — anything that must match something that already exists:
  S3/MinIO access keys, database URLs, tenant/client ids, upstream provider keys,
  endpoint overrides. Fill these in by hand; the tool will not invent them.
- **One key per Secret.** Each Secret gets its own value, so a leak in one
  namespace cannot authenticate against another. Clients therefore hold one key
  per server rather than one fleet-wide key.

## Rotation safety

Every key is classified in the generated file:

| class | meaning | typical case |
|---|---|---|
| `hot` | mounted as a directory, re-read by the app | a Secret the chart mounts and polls |
| `restart` | env-injected, read once at container start | most charts |
| `coordinated` | rotate in lock-step with the backing system | a database password |
| `never` | rotating destroys state the app cannot rebuild | an encryption master key |

The default invocation rotates the **API-key tier only** — the keys clients
present to `/mcp` and `/v1`. Anything more invasive needs a flag:

| command | rotates |
|---|---|
| `fleet_secrets.py` | API keys only (every `*-apikey` Secret and the REST admin key) |
| `--gateway-admin` | + the gateway's `admin-api-keys` |
| `--postgres` | + the database `password` (warns: change it in the database first) |
| `--all` | both of the above |
| `--keep` | nothing — refresh the file in place |
| `--rotate-master-keys --yes` | the encryption master key (destructive) |

Keys classified `never` are never rotated by a default run, and
`--rotate-master-keys` refuses to proceed without `--yes`:

```bash
python3 fleet_secrets.py --rotate-master-keys
#   -> explains the consequence (every stored credential becomes undecryptable
#      and must be re-saved) and exits 2
python3 fleet_secrets.py --rotate-master-keys --yes
#   -> actually rotates
```

Per-key procedures, including the order-of-operations for database passwords:
**[ROTATION.md](ROTATION.md)**.

## Reading the cluster

`--from-cluster` reads the live Secrets so the manifest reflects reality rather
than a possibly-stale values file. It shells out to `k` by default; override with
`--kubectl <cmd>` or `$KUBECTL`.

Because `k` is often a shell *function* (which `subprocess` cannot execute
directly), the resolver tries in order and prints which one it used:

1. the command as a binary on `PATH`
2. `fish -c` — autoloaded functions
3. `fish -i -c` — functions defined only in an interactive config
4. `kubectl` — with a warning, so a wrong context never passes silently

If it cannot read a Secret, `--from-cluster` **exits 2 without writing the
file**. An incomplete cluster view would otherwise produce a manifest that
reverts live values — the failure mode this flag exists to prevent.

## Naming

`--naming-report` compares what you deploy against what the chart defaults
declare:

```
$ python3 fleet_secrets.py --naming-report
APP                    SOURCE   SECRET / KEY
logsearch_mcp          default  logsearch-mcp-apikey/api-keys
logsearch_mcp          site     shared-apikeys/api-keys
```

Divergence is not automatically wrong — sharing one key across several namespaces
is a legitimate choice, it just widens the blast radius of a leak.

## Files

| file | role |
|---|---|
| `fleet_secrets.py` | the generator |
| `registry.yaml` | the tracking table: app → chart, namespace, values files |
| `secrets.fleet.yaml` | generated output — 0600, gitignored, never commit it |
| `verify.py` | renders each chart and diffs its Secret refs against the output |
| `ROTATION.md` | per-secret rotation runbook |

## Adding an app

Add a registry entry, run the generator, run `verify.py`. If a chart falls back
to a name its values never spell out (e.g. `<deployment.name>-apikey`), declare it
under `extra_secrets`:

```yaml
- name: my-mcp
  chart: mcp_servers/my_mcp/helm
  namespace: my-mcp
  values: [mcp_servers/my_mcp/helm/values.yaml]
  extra_secrets:
    - name: my-mcp-apikey
      keys: [api-keys]
```
