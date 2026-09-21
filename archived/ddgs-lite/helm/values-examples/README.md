# ddgs-lite — values examples (secret-free, hardlinked to GitHub)

Paste-ready **full-values** examples for the `ddgs-lite` chart. This folder is hardlinked to the public repository and is intentionally secret-free: no real keys, tokens, passwords, or credentials — only `# SITE:` placeholders and the documented G2 lab domain.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

| File | Target | Posture |
|---|---|---|
| [`values.g2.yaml`](values.g2.yaml) | SE-G2 lab cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`) | Literal domain, HPE corporate proxy on |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | PCAI hosted trial (`${DOMAIN_NAME}`) | PCAI-resolved domain, proxy on (flip for direct-egress clusters) |

Real per-site values — credentials, site-specific tuning — live in `helm/local/` (gitignored, hardlink-ignored, and `.helmignore`d out of chart packaging; never copy them here).

## Using on PCAI

1. Import the packaged chart into the PCAI catalog (once per chart version).
2. Pick the closest example, adjust every `# SITE:` line (endpoint host, image tag, proxy need).
3. Paste the **whole document** into the chart's *Helm Values* editor and apply — it is a complete values document, not an overlay, because the PCAI values editor replaces the chart's bundled `values.yaml` entirely.

## Using with helm (operators)

```bash
helm template <release> helm/ -f helm/values-examples/values.g2.yaml   # eyeball the render
helm upgrade --install <release> helm/ -n <namespace> -f helm/values-examples/values.g2.yaml
```

Key-by-key walkthrough: [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md).
