# k8s-mcp — values examples (secret-free, hardlinked to GitHub)

Paste-ready **full-values** examples for the `k8s-mcp` chart (the trusted-operator chart). This folder is hardlinked to the public repository and is intentionally secret-free: no real keys, tokens, passwords, or credentials — the API key is only ever referenced by **Secret name** (`apiKey.existingSecret`), never as a value.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

| File | Target | Posture |
|---|---|---|
| [`values.g2.yaml`](values.g2.yaml) | SE-G2 lab cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`) | Literal domain, exec enabled (lab posture), KServe/HPE CRD groups granted |

No `values.hosted-trial.yaml` here on purpose: the trusted chart is the HPE-operators distribution — customer-facing deliveries use the structurally locked `helm-customer/` chart, whose examples live in [`../../helm-customer/values-examples/`](../../helm-customer/values-examples/README.md).

Real per-site values (site endpoints, posture) live in `helm/local/` (gitignored, hardlink-ignored, and `.helmignore`d out of chart packaging; never copy them here).

## Using on PCAI

1. Import the packaged chart into the PCAI catalog (once per chart version).
2. **Create the API-key Secret out of band first** — the chart never creates it and the Deployment fails loud until it exists (command in [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md#required-values)).
3. Adjust every `# SITE:` line (endpoint host, Secret name, exec posture, CRD groups).
4. Paste the **whole document** into the chart's *Helm Values* editor and apply — it is a complete values document, not an overlay, because the PCAI values editor replaces the chart's bundled `values.yaml` entirely.

## Using with helm (operators)

```bash
helm template <release> helm/ -f helm/values-examples/values.g2.yaml   # eyeball the render
helm upgrade --install <release> helm/ -n <namespace> -f helm/values-examples/values.g2.yaml
```

Key-by-key walkthrough: [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md).
