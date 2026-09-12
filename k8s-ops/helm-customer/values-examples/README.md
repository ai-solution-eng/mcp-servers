# k8s-mcp-customer — values examples (secret-free, hardlinked to GitHub)

Paste-ready **full-values** examples for the `k8s-mcp-customer` chart — the **structurally locked** customer distribution of the Kubernetes ops MCP server. This folder is hardlinked to the public repository and is intentionally secret-free: the API key is only ever referenced by **Secret name** (`apiKey.existingSecret`), never as a value.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

| File | Target | Posture |
|---|---|---|
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | PCAI hosted trial (`${DOMAIN_NAME}`) | PCAI-resolved domain, locked security posture (exec/policy/RBAC keys do not exist in this chart) |

This chart is the customer-deployable twin — that is why it gets a hosted-trial example while the trusted `helm/` chart does not (its examples live in [`../../helm/values-examples/`](../../helm/values-examples/README.md)). What the frontend CAN configure is deliberately limited to benign wiring: image, naming, service, exposure endpoint, API-key Secret reference, console toggle. Pasting `exec: {enabled: true}` here is inert — nothing in the chart consumes it.

Real per-customer values live in `helm-customer/local/` (gitignored, `.helmignore`d out of chart packaging; never copy them here).

## Using on PCAI

1. Import the packaged `k8s-mcp-customer` chart into the PCAI catalog (once per chart version).
2. **Create the API-key Secret out of band first** — the chart never creates it and the Deployment fails loud until it exists (command in [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md#required-values)).
3. Adjust every `# SITE:` line (endpoint host, Secret name).
4. Paste the **whole document** into the chart's *Helm Values* editor and apply.

## Using with helm (operators)

```bash
helm template <release> helm-customer/ -f helm-customer/values-examples/values.hosted-trial.yaml
helm upgrade --install <release> helm-customer/ -n <namespace> \
  -f helm-customer/values-examples/values.hosted-trial.yaml
```

Day-2 security changes are a platform action from the kube command line — the chart's NOTES.txt prints every command. Full walkthrough: [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md).
