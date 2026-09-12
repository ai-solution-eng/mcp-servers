# helm/values-examples — paste-ready values for PCAI

Example **full values documents** for the SQLhandler Helm chart. Each file is
complete (every chart key listed) and **intentionally secret-free** — every
credential is a `<ROLE_NAME>` placeholder (see *Placeholder convention* below).

| File | Deployment target |
|---|---|
| [`values.g2.yaml`](values.g2.yaml) | **SE G2** site (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`, `project-user-*` namespace, in-cluster MinIO). Sanitized from the real site file; carries the benchmark-derived 4-replica + HPA scaling settings. |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | **Hosted trial** on a customer PCAI: `${DOMAIN_NAME}` endpoint, oauth2-proxy gateway `AuthorizationPolicy` on, credential fillers. |

## Why this folder exists (and why it is secret-free)

- This folder is **hardlink-synced to GitHub** (and into delivery trees) by the
  repo hardlinker — anything here is public. Keep it free of real credentials,
  endpoints that are not generic (in-cluster service DNS names are fine), and
  personal emails.
- The **real per-site values** (credentials, customer endpoints, site catalogs)
  live in **`helm/local/`** — gitignored (`helm/local/*`), ignored by the
  hardlinker (`helm*/local`), and excluded from the packaged chart
  (`helm/.helmignore`: `local/`). See `helm/local/README.md` for the
  out-of-band `kubectl create secret` / rotation convention.

## Using an example the PCAI way (no helm, no kubectl)

1. **Import the packaged chart** (`.tar.gz`) into PCAI once — PCAI users never
   run `helm install` or `kubectl apply`.
2. Open the deployment's **Helm Values** editor (or the PCAI API).
3. **Paste the whole example file** as the values document.
4. **Adjust the `# SITE:` lines** — data source coordinates, the
   `<ROLE_NAME>` credential placeholders, replica/HPA settings, the semantic
   catalog.
5. **Apply.** PCAI resolves `${DOMAIN_NAME}` before rendering. To change
   anything later, edit the same values document and re-apply (an upgrade).

Credentials: prefer creating the Kubernetes Secret out-of-band and leaving
`credentialsSecret.create: false` (the chart default). Only
`values.g2.yaml`'s dev pattern uses `create: true`, which renders the Secret
from the values file — acceptable for throwaway test clusters, never for
shared ones.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

## Using an example as an operator

```bash
# render locally to inspect what the chart would create
helm template sqlhandler helm -f helm/values-examples/values.g2.yaml

# or deploy/upgrade directly (operators only — not the PCAI flow)
helm upgrade --install sqlhandler helm -n <namespace> \
  -f helm/values-examples/values.hosted-trial.yaml
```

## Documentation

- Deployment guide: [`../../documentation/DEPLOYMENT.md`](../../documentation/DEPLOYMENT.md)
- Post-deploy verification: [`../../documentation/VERIFICATION.md`](../../documentation/VERIFICATION.md)
- Field reference for every key: [`../values.yaml`](../values.yaml)
- Semantic catalog spec: [`../../docs/semantic-catalog.md`](../../docs/semantic-catalog.md)
