# skills-mcp

Serves the team's **markdown skills** over MCP 2.0, so any agent can discover
them by name instead of cloning a repo or pasting a 2,000-word prompt.

A *skill* is a directory containing a `SKILL.md`: a name, a one-line
description, and instructions. The skills themselves stay files in git
([`ai-solution-eng/tools`](https://github.com/ai-solution-eng/tools) →
`skills/`). This server is the delivery van.

## Three tools, forever

| Tool | Returns |
| --- | --- |
| `list_skills()` | every skill: `name`, `description`, `has_files` + `count` + `source` |
| `get_skill(name)` | that skill's full markdown, its `files`, resolvable `related` skills |
| `get_skill_file(name, path)` | one file inside a skill (utf8 or base64) |

Skill names are **arguments, never tools**. The PCAI LLM gateway grants MCP
tools per key, default-deny, with no wildcard — one tool per skill would mean
every new skill is invisible and uncallable until a human adds a grant row.
The catalogue is data.

> Status: step 1 skeleton (transport, auth, health). The loader and the three
> tools are the next two steps.

## How skills get in (three layers, one rule)

```
bundled snapshot (baked into the image)  -> seeds the working copy on boot
working copy    (/data/skills-current)   -> what the tools read
GitHub          (same public tarball)    -> replaces the working copy, on demand or every 4 h
```

**The rule: git wins, always.** A refresh *replaces* the working copy — never
merges. It validates before swapping, so a bad pull keeps the old catalogue
serving. The pod is a cache; the repo is the truth.

## Admin endpoints (same API key as /mcp)

| Endpoint | What it does |
| --- | --- |
| `POST /admin/refresh` | fetch `SKILLS_SOURCE_URL/<ref>` → extract only `SKILLS_SOURCE_PATHS` → parse → **abort if 0 skills** → new release dir → atomic symlink swap → prune to 3. Failure = HTTP 502 and the old catalogue keeps serving. |
| `GET /admin/status` | what is served (origin/ref/commit/refreshed_at/count/warnings/source_dir) plus refresh config and the last error. |
| `GET /healthz` | probes, key-free: `{"status","server","skills","origin"}`. |

## Run it locally

```bash
uv venv --python 3.12
uv pip install -e .
SKILLS_MCP_API_KEYS=devkey .venv/bin/skills-mcp --transport streamable-http --port 9101
```

```bash
curl -s localhost:9101/healthz                       # probes are key-free
curl -s -X POST localhost:9101/mcp \
  -H 'Authorization: Bearer devkey' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `SKILLS_MCP_API_KEYS` | *(unset)* | Comma-separated keys for `/mcp`. **Unset = fail open** (local only). |
| `SKILLS_SOURCE_PATHS` | `skills` | Comma list of folders in the repo to load. The demo/team switch. |
| `SKILLS_SOURCE_URL` | *(unset)* | Tarball base URL. Unset = refresh disabled, bundled snapshot only. |
| `SKILLS_SOURCE_REF` | `main` | Branch, tag or commit sha to fetch. |
| `SKILLS_REFRESH_INTERVAL_S` | `14400` | 4 h timer. `0` disables it (the endpoint still works). |
| `SKILLS_ROOT` | `/data` | Where `releases/` and the `skills-current` symlink live. |
| `SKILLS_BUNDLED_ROOT` | `/opt/skills-bundled` | The snapshot baked into the image. |
| `MCP_HOSTNAME` | *(chart sets it)* | Pinned public FQDN. **Required when gateway-fronted** or every request is a 421. |
| `SKILLS_MCP_API_KEYS` | *(unset)* | Keys for `/mcp` **and** `/admin/*`. Unset = fail open (local only). |

## Deploy

`helm/` is a PCAI-ported chart (VirtualService on `istio-system/ezaf-gateway`).
Import it as an EzAppConfig — see `documentation/DEPLOYMENT.md` (step 6).
The server needs **zero Kubernetes RBAC**: it reads files, not the cluster.
