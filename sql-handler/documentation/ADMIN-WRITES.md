# Enabling admin writes (writable policy + catalog edits)

**Symptom this fixes.** Signed in as an admin, the panel refuses writes:

| Surface | Error |
|---|---|
| Semantic-catalog editor | `writes require a credential: SQLHANDLER_API_KEYS is configured …` (HTTP 401) |
| Policy editor | `cannot write the policy file (/etc/sqlhandler-policy/policy.json): [Errno 30] Read-only file system` (HTTP 503) |
| Key mint (grant binding) | same `[Errno 30] Read-only file system` |

**Two independent root causes.** Both are fixed in 3.9.1+, but the second one
also needs a **deployment change** that no code fix can make for you.

| # | Cause | Fixed by |
|---|---|---|
| 1 | The write gate only accepted a presented **API key**. An SSO-signed-in admin presents none, so the gate refused the very admin who may rewrite the ACL. | Server code (3.9.1+): a policy-designated admin is admitted through the identity ladder. |
| 2 | The policy file is mounted from a **ConfigMap**, which is read-only by construction. | **Chart values change** (below) — switch to claim mode. |

### Also fixed in the same release: saved queries were not durable

Not one of the three symptoms above, but found while tracing the same wiring.
Saved queries (`SQLHANDLER_SAVED_QUERIES_PATH`) had no chart-set path, so
they fell back to `SQLHANDLER_CACHE_DIR` — `/tmp/sqlhandler-cache`, an
**emptyDir**. Consequences: every saved query was lost on pod restart, and
each replica kept a private copy (a query saved through one replica was
invisible to the others). The catalog store had been given a PVC path; saved
queries had not.

With `semanticCatalog.store.enabled: true` the chart now points them at the
same claim (`/var/lib/sqlhandler-catalog/saved-queries.json`). Override with
`semanticCatalog.store.savedQueriesPath`; leave it `""` with the store
disabled to keep the old pod-local behaviour. The startup log states which
posture is in force either way.

Nothing to migrate — anything already saved lived on an emptyDir and is gone.

---

## Deployment is now a single step

The chart **creates the policy claim itself** (`security.policy.claim.create:
true` is the default). There is no PVC to apply by hand and no ordering to
get right:

```bash
helm upgrade sqlhandler <chart> -n sqlhandler -f your-values.yaml
```

That is the whole procedure. Three things to know:

* **Your minted keys and catalog survive upgrades now.** Every chart-created
  PVC carries `helm.sh/resource-policy: keep`, so Helm never deletes the
  volume. Before this fix a redeploy could recreate `sqlhandler-catalog`
  (new UID, empty filesystem) and **destroy every minted API key**, the
  uploaded catalog and saved queries — callers saw "my key stopped working"
  with nothing in any log to explain it. Losing the *policy* claim is worse:
  a missing policy file means the permissive default, so the deployment
  would silently lose its restrictions. Deleting a volume is now only ever a
  deliberate `kubectl delete pvc`.

  To confirm on a live deployment:

  ```bash
  kubectl -n sqlhandler get pvc -o custom-columns=\\
  NAME:.metadata.name,CREATED:.metadata.creationTimestamp,KEEP:.metadata.annotations.helm\\.sh/resource-policy
  # KEEP must read "keep" on every row.
  ```

* **A site that already manages its own claim keeps working unchanged** — an
  explicit `security.policy.existingClaim` wins over the default, and no
  claim is created for it. (Set `claim.create: false` too if you want the
  values to read unambiguously.)
* **A missing policy file is a PERMISSIVE default, not an error.** A fresh
  install comes up *serving*: every table visible, no column masked. That is
  intentional — the pod must be reachable so you can author restrictions in
  the admin panel. The startup log says so loudly:

  ```
  WARNING POLICY IS NOT RESTRICTING ANYTHING: /etc/sqlhandler-policy/policy.json
  does not exist, so the server is serving the PERMISSIVE DEFAULT ...
  ```

  Restrict access by authoring the policy (panel, or the seed ConfigMap on a
  fresh claim). Once a valid file exists it is enforced and hot-reloaded.

| Policy file state | Data access | Pod ready? |
|---|---|---|
| **missing** | permissive — nothing hidden or masked | ✅ yes (bootstrap) |
| **present but invalid** | fails closed — every covered table hidden | ✅ yes (reachable to fix) |
| **present and valid** | enforced | ✅ yes |

---

## Step 1 — confirm which cause you are hitting

```bash
# The read-only posture is now visible at startup and on /health.
kubectl -n sqlhandler logs deploy/sqlhandler | grep -i "policy file is"
#   WARNING banner = cause 2 (read-only mount) — do Step 2.
#   INFO line      = policy is writable; cause 2 does not apply.

kubectl -n sqlhandler get deploy sqlhandler -o jsonpath='{.spec.template.spec.volumes[?(@.name=="policy")]}'
#   {"configMap":{...}}  -> ConfigMap mode (read-only): cause 2.
#   {"persistentVolumeClaim":{...}} -> claim mode (writable): already fixed.
```

`/health` now carries the same answer:

```bash
kubectl -n sqlhandler port-forward deploy/sqlhandler 9097:9097 &
curl -s localhost:9097/health | jq .policy
# {"enabled":true,"loaded":true,"writable":false,...}   <- read-only
```

---

## Step 2 — switch the policy to a writable volume (claim mode)

The chart can now create the volume itself (`security.policy.claim.create`),
so there is no hand-applied PVC to remember.

```yaml
security:
  policy:
    enabled: true
    existingConfigMap: sqlhandler-policy   # still required: the SEED source
    existingConfigMapKey: policy.json
    seedFromConfigMap: true                # copy it to the claim on first boot
    claim:
      create: true                         # chart renders <release>-policy
      accessModes: [ReadWriteMany]         # REQUIRED with replicas > 1
      storageClass: ""                     # "" = cluster default
      size: 1Gi
    mountPath: /etc/sqlhandler-policy
    fileName: policy.json
```

**Why `ReadWriteMany`:** the policy is ONE live file that every replica
hot-reloads by mtime. A `ReadWriteOnce` claim attaches on a single node, so the
other replicas sit `Pending` **and** replicas disagree about the ACL. The chart
fails the render rather than ship that.

**Already have a claim?** Set `security.policy.existingClaim: <name>` instead of
`claim.create`, and leave `claim.create: false`. Setting both fails the render.

### What happens on first boot

`seedFromConfigMap` copies the ConfigMap document to the claim **once**, when
the live file does not exist yet. The copy is atomic (temp file + rename), so a
second replica booting concurrently never observes a half-written policy.

After seeding, **the file on the claim is the truth forever** — later ConfigMap
edits stop applying. That is the trade of writable-policy mode: the panel owns
the document from then on.

> **Do not delete the ConfigMap.** It stays the seed source, and it is your
> recovery copy if the claim is ever lost.

### Apply

```bash
helm upgrade sqlhandler <chart> -n sqlhandler -f your-values.yaml
kubectl -n sqlhandler rollout status deploy/sqlhandler
```

Verify:

```bash
kubectl -n sqlhandler logs deploy/sqlhandler | grep -i "policy seeded\|policy file is"
# "policy seeded: /etc/sqlhandler-policy-seed/policy.json -> /etc/sqlhandler-policy/policy.json"
# "policy file is WRITABLE (/etc/sqlhandler-policy/policy.json): admin panel edits persist."
```

---

## Step 3 — verify the admin surfaces

1. Open the UI, sign in through SSO (your subject must be in the policy
   `admins` list — check with the Access-control panel).
2. Edit a virtual table in the **semantic catalog** → save. It should persist
   with **no API key pasted**.
3. **Policy** tab → change a grant → save. It should persist.
4. **Access control** → mint a key. The mint's grant binding writes the policy,
   so this now succeeds too.

Check the saved-query posture landed:

```bash
kubectl -n sqlhandler logs deploy/sqlhandler | grep -i "saved queries"
# "Saved queries persist to /var/lib/sqlhandler-catalog/saved-queries.json
#  (durable: an explicit path is configured)."
# If it instead warns "NOT durable", the chart value did not take effect.
```

If you still get 401 on the catalog but the policy editor works, your subject
is not a designated admin — add it to `admins` (Step 4).

---

## Step 4 — bootstrap / repair by hand (kubectl)

Needed only when the panel cannot be used (e.g. you locked yourself out).

```bash
# Read the live policy (claim mode) or the seed (ConfigMap mode)
kubectl -n sqlhandler exec deploy/sqlhandler -- cat /etc/sqlhandler-policy/policy.json

# Edit it in place (claim mode only — this is the writable volume)
kubectl -n sqlhandler exec -it deploy/sqlhandler -- sh -c \
  'cat > /etc/sqlhandler-policy/policy.json' < my-policy.json
# The server hot-reloads on mtime; no restart needed.
```

**ConfigMap mode only** (if you deliberately stay read-only):

```bash
kubectl -n sqlhandler create configmap sqlhandler-policy \
  --from-file=policy.json=my-policy.json --dry-run=client -o yaml | kubectl apply -f -
# kubelet syncs the mount and the engine hot-reloads on mtime.
```

### Designating an admin

`admins` accepts **subjects** and **key fingerprints**:

```json
{
  "admins": ["subject:andrew-bydlon", "key:sha256:b1a47ad2d71c"],
  "default_group": "demo_default",
  "groups": { "demo_default": { "hidden_tables": ["dept_payroll"] } },
  "subjects": { "andrew-bydlon": ["compensation_access"] }
}
```

Both spellings of a subject match (`andrew-bydlon` and
`subject:andrew-bydlon`); the `key:` prefix is normalized to the bare
fingerprint. Find a key's fingerprint in the Access-control panel.

---

## Rollback

The old read-only behaviour comes back by reverting the values and
re-upgrading. **Your policy edits live on the claim** — export them first:

```bash
kubectl -n sqlhandler exec deploy/sqlhandler -- cat /etc/sqlhandler-policy/policy.json > policy-backup.json
```

Then set `claim.create: false`, `existingClaim: ""`, re-apply, and push the
backed-up document into the ConfigMap (the ConfigMap-mode command above).

The claim itself is not deleted by a downgrade unless you remove it — check
with `kubectl -n sqlhandler get pvc`.
