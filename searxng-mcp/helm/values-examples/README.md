# searxng-mcp — values examples (secret-free, hardlinked to GitHub)

Paste-ready **full-values** examples for the `searxng-mcp` chart. This folder is hardlinked to the public repository and is intentionally secret-free: no real keys, tokens, passwords, or credentials — `searxng.secretKey` is always a `<SECRET_KEY>` placeholder, never a real value.

| File | Target | Posture |
|---|---|---|
| [`values.g2.yaml`](values.g2.yaml) | SE-G2 lab cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`) | Literal domain, corporate proxy on, browser sidecar on, MITM CA wired |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | PCAI hosted trial (`${DOMAIN_NAME}`) | PCAI-resolved domain, browser sidecar off (enable if JS pages matter), proxy on |

Real per-site values — the actual `searxng.secretKey` and any site-specific tuning — live in `helm/local/` (gitignored, hardlink-ignored, and `.helmignore`d out of chart packaging; never copy them here).

## Using on PCAI

1. Import the packaged chart into the PCAI catalog (once per chart version).
2. **Two secrets are in play:**
   - `searxng.secretKey` (**required** — the render fails without it or
     `searxng.existingSecret`): the examples use a `<SECRET_KEY>` placeholder;
     replace it, or pre-deploy a Secret and point `searxng.existingSecret` at
     it (preferred — the value then never passes through values):
     `kubectl -n <ns> create secret generic searxng-secret --from-literal=secret=$(openssl rand -hex 32)`
   - the **optional** MCP API key: both examples point `apiKey.existingSecret`
     at the fleet convention `mcp-fleet-apikeys` (key `api-keys`) — create it
     the same way to switch the key gate on
     (`kubectl -n <ns> create secret generic mcp-fleet-apikeys --from-literal='api-keys=<key1>,<key2>'`),
     or clear `apiKey.existingSecret` to run `/mcp` open with a loud startup
     warning.
3. Pick the closest example, adjust every `# SITE:` line (endpoint host, secretKey, proxy need, browser sidecar).
4. Paste the **whole document** into the chart's *Helm Values* editor and apply — it is a complete values document, not an overlay, because the PCAI values editor replaces the chart's bundled `values.yaml` entirely.

## Using with helm (operators)

```bash
helm template <release> helm/ -f helm/values-examples/values.g2.yaml   # eyeball the render
helm upgrade --install <release> helm/ -n <namespace> -f helm/values-examples/values.g2.yaml
```

Key-by-key walkthrough: [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md).
