# Mobius Chat — Cloud Run deployment runbook

Clean-slate deployment package, built 2026-04-20. Assumes GCP Secret
Manager is already populated (`jwt-secret`, `db-password`,
`groq-api-key`, `anthropic-api-key`, `app-secret-key`) and the
`mobius-platform-dev@mobius-os-dev.iam.gserviceaccount.com` service
account exists with `secretmanager.secretAccessor` bound on each.

This file is the single operational reference. When something doesn't
match what you see in GCP, the GCP state is the truth — update the
script or config, then update this doc.

---

## Architecture

One Cloud Run service (`mobius-chat`) running a Python 3.13 container:

- FastAPI on port 8080 via uvicorn
- In-process background worker thread (`CHAT_QUEUE_TYPE=memory`)
- Cloud SQL via Unix-socket IAM auth (no password env-var)
- Secret Manager via Python SDK (`app/secrets_loader.py`)
- Skills-core, contracts, and retriever vendored into the image

Post-beta split plan: when concurrency exceeds what one instance
handles cleanly, split into `chat-api` (HTTP only) + `chat-worker`
(queue consumer) with Redis between them. Today, one service.

---

## Files in this package

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage build, Python 3.13-slim, non-root runtime |
| `.dockerignore` | Keep `.env`, `.venv`, tests, docs out of the image |
| `requirements.txt` | Pinned runtime deps (audit policy in file header) |
| `deploy/dev.env` | Non-secret config for `mobius-os-dev` |
| `scripts/bootstrap_gcp.sh` | One-time: enable APIs, AR repo, IAM, DB user |
| `scripts/migrate.sh` | Apply `db/schema/*.sql` to Cloud SQL |
| `scripts/deploy.sh` | Build image (Cloud Build) + deploy Cloud Run |

---

## First deploy — step by step

Target: `mobius-os-dev` project, `us-central1` region.

### 0. Prerequisites

```bash
gcloud auth login
gcloud auth application-default login       # required by migrate.sh
gcloud config set project mobius-os-dev
```

Install cloud-sql-proxy (for `migrate.sh` only):

```bash
brew install cloud-sql-proxy
```

### 1. Bootstrap project IAM + infra

Enables APIs, creates Artifact Registry repo, creates the Cloud SQL
IAM database user, grants the runtime service account the roles it
needs. Idempotent — safe to re-run.

```bash
cd mobius-chat
scripts/bootstrap_gcp.sh dev
```

What this does **not** do: create the Cloud SQL instance (already
exists: `mobius-platform-dev-db`), create secrets (already in Secret
Manager), deploy the service.

### 2. Apply schema migrations

Runs Cloud SQL Auth Proxy locally, applies every `db/schema/*.sql`
file in lexical order, records each in a `schema_migrations` ledger
table. Rerunning is a no-op.

```bash
scripts/migrate.sh dev --dry-run    # see what's pending
scripts/migrate.sh dev              # apply
```

If you see `cloud-sql-proxy failed to bind socket`, re-run
`gcloud auth application-default login` and try again.

### 3. Deploy

Builds the image via Cloud Build (GCP-side, no local Docker required)
and deploys to Cloud Run with every flag explicit. The first deploy
auto-populates `CHAT_CORS_ORIGINS` with the service's assigned
`*.run.app` URL.

```bash
scripts/deploy.sh dev --dry-run     # print commands, don't run
scripts/deploy.sh dev               # real deploy
```

Deploy output ends with the service URL. Smoke-test immediately:

```bash
URL=$(gcloud run services describe mobius-chat \
  --project=mobius-os-dev --region=us-central1 \
  --format='value(status.url)')

curl "${URL}/health"   # {"status":"ok"}
curl "${URL}/ready"    # {"status":"ready","checks":{...}}
```

If `/ready` returns 503, the `checks` object names the failing
dependency.

### 4. Persist CORS value in `deploy/dev.env`

First deploy prints a hint like:

> Persist this in deploy/dev.env for future deploys:
>   CHAT_CORS_ORIGINS=https://mobius-chat-xxxxx-uc.a.run.app

Copy that line into `deploy/dev.env` and commit. Subsequent deploys
won't overwrite it.

---

## Subsequent deploys

Code-only change:

```bash
scripts/deploy.sh dev
```

Config-only change (no new image, just redeploy):

```bash
# Edit deploy/dev.env, commit, then:
scripts/deploy.sh dev --skip-build
```

Schema change (new `db/schema/NNN_*.sql` file):

```bash
scripts/migrate.sh dev --dry-run
scripts/migrate.sh dev
scripts/deploy.sh dev               # only if app code changed too
```

---

## Rollback

Cloud Run keeps every revision until you delete it. Revert by
shifting traffic — no rebuild, no downtime.

### List recent revisions

```bash
gcloud run revisions list \
  --service=mobius-chat \
  --project=mobius-os-dev \
  --region=us-central1 \
  --limit=10
```

### Send 100% traffic to a previous revision

```bash
gcloud run services update-traffic mobius-chat \
  --project=mobius-os-dev \
  --region=us-central1 \
  --to-revisions=mobius-chat-00042-abc=100
```

### Gradual rollout (10% new, 90% old)

```bash
gcloud run services update-traffic mobius-chat \
  --project=mobius-os-dev \
  --region=us-central1 \
  --to-revisions=mobius-chat-00043-def=10,mobius-chat-00042-abc=90
```

Watch Cloud Logging + error rate for 15 min before shifting more.

---

## Secrets

Managed separately from this package. To rotate a secret:

```bash
# 1. Add a new version
echo -n "${NEW_VALUE}" | gcloud secrets versions add groq-api-key \
  --project=mobius-os-dev --data-file=-

# 2. Redeploy to pick it up (Cloud Run caches secret values per
#    revision; changing the version doesn't retroactively update
#    running instances).
scripts/deploy.sh dev --skip-build
```

The `:latest` pinning in `deploy.sh` always fetches the newest version
at the moment Cloud Run starts the container.

---

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `StartupAssertionError: CHAT_RAG_DATABASE_URL does not use /cloudsql/` | Wrong DB URL or missing `--add-cloudsql-instances` | Check `CLOUDSQL_INSTANCE` in env file; redeploy |
| `secret_manager_fetch_failed env=GROQ_API_KEY` | SA missing `secretAccessor` role on secret | `gcloud secrets add-iam-policy-binding groq-api-key --member=serviceAccount:...` |
| `/ready` returns 503, `checks.db.status=fail` | IAM user missing on DB / missing privileges | Re-run `scripts/bootstrap_gcp.sh dev` |
| Container crashes on boot with `ImportError: mobius_skills_core` | Sibling not vendored in image | Check `.dockerignore` allow-list; rebuild |
| LLM calls fail with `permission denied` | SA missing `aiplatform.user` | Re-run `scripts/bootstrap_gcp.sh dev` |
| Chat hits 90s deadline on every request | DB IO slow (e.g. cold pool) | First few requests after deploy; check again in 5 min |

Logs:

```bash
gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=mobius-chat' \
  --project=mobius-os-dev \
  --format='value(timestamp,severity,textPayload)' \
  --limit=100
```

---

## What's intentionally not set up yet

Listed so nobody wonders later:

- **Custom domain** — beta runs on the `*.run.app` URL. Mapping to
  something like `chat.mobius-os-dev.example` is a post-beta task
  (`gcloud run domain-mappings create`).
- **VPC egress** — the `mobius-dev-vpc-connector` exists but isn't
  attached to the service. Add `--vpc-connector` + `--vpc-egress=all`
  to `deploy.sh` when skills-mcp / task-manager come online on
  private IPs.
- **Cloud Build trigger** — deploys are manual via `deploy.sh`. Add
  a `cloudbuild.yaml` + trigger when deploys happen >2× / week.
- **Uptime check on `/ready`** — one-click in the Cloud Monitoring
  console; not scripted because it's a one-time setup.
- **Budget alert** — set manually in Billing. ~$200/mo floor given
  current Cloud SQL + 1 min-instance; alert at $500.
- **`MOBIUS_TASK_MANAGER_PROMOTION=1`** — task-manager isn't
  deployed yet. Flip the env var (and the URL) when it is.
- **`MOBIUS_OS_AUTH_URL`** — JWT auth disabled until mobius-os
  runs somewhere the Cloud Run service can reach.
- **Structured JSON logging** — Batch 4 work. Today's logs are
  plain text; Cloud Logging still parses timestamps + severity.
- **LLM retry with backoff** — Batch 4 work. Transient 5xx = user
  sees one failure; acceptable for beta.

---

## When you're ready for prod

1. Copy `deploy/dev.env` → `deploy/prod.env`, change:
   - `GCP_PROJECT` to the prod project
   - `CLOUDSQL_INSTANCE`, `CLOUDSQL_DATABASE` to prod values
   - `SERVICE_ACCOUNT` to the prod runtime SA
   - `IMAGE_BASE` to the prod Artifact Registry path
   - `RUN_MIN_INSTANCES` + `RUN_MAX_INSTANCES` per prod capacity plan
2. Enable billing on the prod project if not already.
3. `scripts/bootstrap_gcp.sh prod && scripts/migrate.sh prod && scripts/deploy.sh prod`
4. Keep dev deployed as a canary for future changes.
