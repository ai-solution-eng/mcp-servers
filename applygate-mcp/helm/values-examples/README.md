# applygate-mcp — values examples (secret-free, hardlinked to GitHub)

Paste-ready, complete, intentionally **secret-free** example values for this
chart. This folder is hardlinked to the public GitHub mirror, so nothing
site-private may live here.

- **Real per-site values live in `helm/local/`** — gitignored,
  hardlink-ignored, and excluded from the packaged chart (`.helmignore`).
  Never commit credentials or cluster-private endpoints into this folder.
- **On PCAI**: import the packaged chart once, open the chart's **Helm
  Values** editor, paste one of these files in full, adjust every
  `# SITE:` line, and apply. PCAI replaces the chart's bundled values.yaml
  entirely, which is why these are full documents with every key present.
- **Operators** (plain Helm) can render/eyeball then install:
  `helm template applygate-mcp helm/ -f helm/values-examples/values.g2.yaml`
  and `helm upgrade --install applygate-mcp helm/ -n <namespace> -f ...`.
- Values walkthrough (required vs optional, gateway, RBAC bootstrap,
  upgrades): [../../documentation/DEPLOYMENT.md](../../documentation/DEPLOYMENT.md).

| File | For |
|---|---|
| `values.g2.yaml` | The HPE SE-G2 lab cluster (literal G2 endpoints, sanitized) — the working reference. |
| `values.hosted-trial.yaml` | A customer hosted-trial environment — `${DOMAIN_NAME}` placeholders where the PCAI build resolves them, neutral namespace allowlist. |

Sanitization rule for anything added here: no keys, tokens, passwords, or
usernames — only role-named `<UPPER_SNAKE>` placeholders (e.g. `<ALLOWED_NAMESPACES>`,
`<IN_CLUSTER_PROMETHEUS_URL>`, `<USERNAME>`) and the already-public G2 endpoint
names. `${DOMAIN_NAME}` is the one non-user placeholder — PCAI substitutes it.
