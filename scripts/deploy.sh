#!/usr/bin/env bash
#
# Mobius Chat — Cloud Run deploy driver.
#
# Usage:
#     scripts/deploy.sh <env>                # build image + deploy + smoke
#     scripts/deploy.sh <env> --dry-run      # print commands, don't run
#     scripts/deploy.sh <env> --skip-build   # redeploy previous image
#     scripts/deploy.sh <env> --skip-smoke   # skip post-deploy smoke (NOT recommended)
#
# Where <env> is a label matching deploy/<env>.env (dev | prod).
#
# What it does
# ------------
# 1. Loads non-secret config from deploy/<env>.env
# 2. Tags a new container image from the current git SHA
# 3. Builds via ``gcloud builds submit`` so the build runs in GCP
#    (faster than local ``docker build + gcloud artifacts push`` and
#    doesn't require Docker on the dev laptop)
# 4. Deploys to Cloud Run with every flag spelled out (no hidden
#    defaults) — someone reading the rollout log can reconstruct the
#    exact service config
# 5. Refreshes CHAT_CORS_ORIGINS post-deploy with the service's
#    allocated *.run.app URL (first deploy only — subsequent deploys
#    preserve whatever's already set)
#
# Safety
# ------
# * ``set -euo pipefail`` + trap on ERR. One failed gcloud call aborts
#   the whole script.
# * No destructive commands. This script never deletes services /
#   revisions / secrets. Rollback is a separate flow (see README).
# * --dry-run prints every command that would run, exits 0 without
#   executing. Use it before the first real deploy to sanity-check
#   the resolved config.

set -euo pipefail

# ── Argument parsing ────────────────────────────────────────────────

ENV_LABEL="${1:-}"
if [[ -z "${ENV_LABEL}" ]]; then
    echo "usage: $0 <env> [--dry-run] [--skip-build] [--skip-smoke]" >&2
    exit 64
fi

DRY_RUN=0
SKIP_BUILD=0
SKIP_SMOKE=0
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --skip-smoke) SKIP_SMOKE=1; shift ;;
        *) echo "unknown flag: $1" >&2; exit 64 ;;
    esac
done

# ── Paths ───────────────────────────────────────────────────────────
# CHAT_DIR = mobius-chat repo root (the dir containing this script's
# parent). PARENT_DIR = Mobius monorepo root (build context, because
# the Dockerfile vendors sibling repos).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${CHAT_DIR}/.." && pwd)"

ENV_FILE="${CHAT_DIR}/deploy/${ENV_LABEL}.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "error: config file not found: ${ENV_FILE}" >&2
    exit 66
fi

# ── Load config ─────────────────────────────────────────────────────
# Use ``set -a`` so every variable in the env file is exported
# automatically — matches the dotenv semantics the app itself uses.
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

# Sanity: required vars from the env file. Missing any of these is
# a deploy-time bug, not a runtime bug.
for required in GCP_PROJECT GCP_REGION SERVICE_NAME SERVICE_ACCOUNT \
                AR_REPO IMAGE_BASE CLOUDSQL_INSTANCE RUN_MEMORY \
                RUN_CPU RUN_CONCURRENCY RUN_TIMEOUT \
                RUN_MIN_INSTANCES RUN_MAX_INSTANCES; do
    if [[ -z "${!required:-}" ]]; then
        echo "error: ${required} missing from ${ENV_FILE}" >&2
        exit 65
    fi
done

# ── Image tag ───────────────────────────────────────────────────────
# Git SHA + timestamp. SHA alone would be enough for uniqueness but
# the timestamp makes ``gcloud artifacts docker images list`` sortable
# chronologically, which is useful during a fast-moving beta.
GIT_SHA="$(git -C "${CHAT_DIR}" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)"
BUILD_TS="$(date -u +%Y%m%d-%H%M%S)"
IMAGE_TAG="${IMAGE_BASE}:${BUILD_TS}-${GIT_SHA}"

# ── Pre-flight: which HEAD are we actually building? ────────────────
# 2026-04-25 — RAG agent hit a worktree gotcha where the deploy ran
# against an older HEAD because the working dir resolved to a git
# worktree, not the main repo. These three lines catch that instantly:
# operator sees the SHA, branch, and dirty/clean state before the
# build submits, and can abort if it doesn't match what they expect.
GIT_BRANCH="$(git -C "${CHAT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY="$(git -C "${CHAT_DIR}" diff --quiet 2>/dev/null && git -C "${CHAT_DIR}" diff --cached --quiet 2>/dev/null && echo clean || echo DIRTY)"
echo "▸ Deploying from HEAD: ${GIT_SHA}  (branch: ${GIT_BRANCH}, working tree: ${GIT_DIRTY})"
echo "▸ Repo path: ${CHAT_DIR}"
if [[ "${GIT_DIRTY}" == "DIRTY" ]]; then
    echo "  warn: working tree has uncommitted changes — image will reflect committed HEAD only" >&2
fi

# ── Helpers ─────────────────────────────────────────────────────────

# Echo the command, then run it (or skip in dry-run).
run() {
    echo "+ $*"
    if [[ "${DRY_RUN}" -eq 0 ]]; then
        "$@"
    fi
}

# Build a delimited key=value list for gcloud run deploy --set-env-vars.
# Uses gcloud's escaped-delimiter form (``^;^KEY1=v1;KEY2=v2...``) so
# values that themselves contain commas (e.g. CHAT_CORS_ORIGINS with a
# multi-origin list) parse correctly. The leading ``^;^`` tells gcloud
# to use ``;`` as the pair separator instead of the default ``,``.
#
# Why ``;`` and not ``@`` or ``|``: ``@`` clashes with Postgres-URL
# user-host separators (``postgres@/mobius_chat?host=...``); ``|``
# tends to appear in regex configs and shell pipelines. Semicolon is
# safe across every value we ship today.
csv_env() {
    printf '^;^%s' "$(printf '%s\n' "$@" | paste -sd';' -)"
}

# ── Build ───────────────────────────────────────────────────────────

if [[ "${SKIP_BUILD}" -eq 0 ]]; then
    echo "── Building ${IMAGE_TAG} ──"
    # Build context is PARENT_DIR so the Dockerfile can COPY siblings.
    # ``--config`` + an explicit cloudbuild.yaml replaces the ``--tag``
    # shortcut (which requires Dockerfile-at-context-root; we don't).
    run gcloud builds submit "${PARENT_DIR}" \
        --project="${GCP_PROJECT}" \
        --region="${GCP_REGION}" \
        --config="${CHAT_DIR}/deploy/cloudbuild.yaml" \
        --ignore-file="${CHAT_DIR}/deploy/.gcloudignore" \
        --substitutions="_IMAGE=${IMAGE_TAG},_IMAGE_BASE=${IMAGE_BASE},_DOCKERFILE=mobius-chat/Dockerfile" \
        --timeout=30m \
        || {
            echo "error: gcloud builds submit failed. Check the build log URL above." >&2
            exit 70
        }
else
    # Resolve the newest previously-built image for this service.
    IMAGE_TAG="$(gcloud artifacts docker images list \
        "${IMAGE_BASE}" --project="${GCP_PROJECT}" \
        --include-tags --format='value(IMAGE,TAGS)' \
        --sort-by="~UPDATE_TIME" --limit=1 | awk '{print $1":"$2}' | awk -F, '{print $1}')"
    if [[ -z "${IMAGE_TAG}" ]]; then
        echo "error: --skip-build set but no prior image found in ${IMAGE_BASE}" >&2
        exit 71
    fi
    echo "── Reusing previous image ${IMAGE_TAG} ──"
fi

# ── Assemble env-var and secret flags ───────────────────────────────
# We list them explicitly (not ``--env-vars-file``) so the command is
# self-documenting in the rollout log.

SET_ENV_VARS=(
    "CHAT_ENV=${CHAT_ENV}"
    "CHAT_ENV_STRICT=${CHAT_ENV_STRICT}"
    "MOBIUS_PROD=${MOBIUS_PROD}"
    "CHAT_QUEUE_TYPE=${CHAT_QUEUE_TYPE}"
    # 2026-04-27 — Memorystore Redis URL for the queue. Required when
    # CHAT_QUEUE_TYPE=redis. Reached via VPC connector below.
    "REDIS_URL=${REDIS_URL:-}"
    "CHAT_DB_MODE=${CHAT_DB_MODE:-}"
    "MOBIUS_TURN_DEADLINE_S=${MOBIUS_TURN_DEADLINE_S}"
    "CHAT_MAX_REQUEST_BYTES=${CHAT_MAX_REQUEST_BYTES}"
    "VERTEX_PROJECT_ID=${VERTEX_PROJECT_ID}"
    "CHAT_RAG_DATABASE_URL=${CHAT_RAG_DATABASE_URL}"
    "CHAT_SKILLS_TASK_MANAGER_URL=${CHAT_SKILLS_TASK_MANAGER_URL}"
    "CHAT_SKILLS_MCP_URL=${CHAT_SKILLS_MCP_URL}"
    "CHAT_SKILLS_DOC_READER_URL=${CHAT_SKILLS_DOC_READER_URL:-}"
    "WEB_SCRAPER_URL=${WEB_SCRAPER_URL:-}"
    "GOOGLE_SEARCH_URL=${GOOGLE_SEARCH_URL:-}"
    "CHAT_SKILLS_HEALTHCARE_URL=${CHAT_SKILLS_HEALTHCARE_URL:-}"
    "CHAT_SKILLS_INSTANT_RAG_URL=${CHAT_SKILLS_INSTANT_RAG_URL:-}"
    "CHAT_SKILLS_EMAIL_URL=${CHAT_SKILLS_EMAIL_URL:-}"
    "MOBIUS_RAG_URL=${MOBIUS_RAG_URL:-}"
    "CHAT_SKILLS_VIBE_URL=${CHAT_SKILLS_VIBE_URL:-}"
    "RAG_API_BASE=${RAG_API_BASE:-}"
    # RAG_API_URL — read by the curator tools (lookup_authoritative_sources
    # + ingest_url) and the legacy RAG-API retrieval path. See dev.env note.
    "RAG_API_URL=${RAG_API_URL:-}"
    # OS_API_URL — mobius-os gateway. corpus_search skill posts to
    # {OS_API_URL}/api/v1/skills/corpus_search with X-Caller=chat header.
    "OS_API_URL=${OS_API_URL:-}"
    "CHROMA_HOST=${CHROMA_HOST:-}"
    "CHROMA_PORT=${CHROMA_PORT:-}"
    "CHROMA_SSL=${CHROMA_SSL:-}"
    "CHROMA_COLLECTION=${CHROMA_COLLECTION:-}"
    # Routes the published-RAG dispatcher to the ChromaDB backend. Must
    # be set for the new retriever integration to hit Chroma and not
    # fall through to the Vertex Vector Search code path.
    "CHAT_VECTOR_STORE=${CHAT_VECTOR_STORE:-}"
    # Latency hardening (2026-04-22). See deploy/dev.env for rationale
    # on each. Empty-default so un-set vars don't blow up the csv.
    "VERTEX_HTTP_TIMEOUT_SECONDS=${VERTEX_HTTP_TIMEOUT_SECONDS:-}"
    "VERTEX_TOTAL_DEADLINE_SECONDS=${VERTEX_TOTAL_DEADLINE_SECONDS:-}"
    "CHAT_DB_POOL_MAX=${CHAT_DB_POOL_MAX:-}"
    "MOBIUS_POST_RUN_ADJUDICATE_EVERY_N=${MOBIUS_POST_RUN_ADJUDICATE_EVERY_N:-}"
    "MOBIUS_MCP_AUTOREGISTER=${MOBIUS_MCP_AUTOREGISTER:-}"
    # Cache-assist (2026-04-23). Empty-default so unset vars don't
    # break the csv; see deploy/dev.env for rationale on each.
    "CACHE_ASSIST_ENABLED=${CACHE_ASSIST_ENABLED:-}"
    "CACHE_ASSIST_BYPASS_PCT=${CACHE_ASSIST_BYPASS_PCT:-}"
    "CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS=${CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS:-}"
    "CACHE_ASSIST_CHROMA_COLLECTION=${CACHE_ASSIST_CHROMA_COLLECTION:-}"
    "CACHE_ASSIST_WRITE_QUALITY_FLOOR=${CACHE_ASSIST_WRITE_QUALITY_FLOOR:-}"
    # Rate limiting (2026-04-23, tiered).
    "CHAT_RATE_LIMIT_PER_MINUTE=${CHAT_RATE_LIMIT_PER_MINUTE:-}"
    "CHAT_RATE_LIMIT_THREAD_PER_MINUTE=${CHAT_RATE_LIMIT_THREAD_PER_MINUTE:-}"
    "CHAT_RATE_LIMIT_USER_PER_MINUTE=${CHAT_RATE_LIMIT_USER_PER_MINUTE:-}"
    "RATE_LIMIT_EXEMPT_IPS=${RATE_LIMIT_EXEMPT_IPS:-}"
    # Dev-only token minter (Sprint 1 #5). MUST be 0 in prod.
    "MOBIUS_DEV_TOKEN_ENABLED=${MOBIUS_DEV_TOKEN_ENABLED:-}"
    "MOBIUS_DEV_TOKEN_TTL_SECONDS=${MOBIUS_DEV_TOKEN_TTL_SECONDS:-}"
    # Model profile (Sprint 2 #0, 2026-04-24).
    "MOBIUS_MODEL_PROFILE=${MOBIUS_MODEL_PROFILE:-}"
    "MOBIUS_ADMIN_ENABLED=${MOBIUS_ADMIN_ENABLED:-}"
    # Tracing (Sprint 1 #11, 2026-04-24).
    "CHAT_TRACE_ENABLED=${CHAT_TRACE_ENABLED:-}"
    "TRACE_SAMPLE_RATIO=${TRACE_SAMPLE_RATIO:-}"
    "OTEL_SERVICE_NAME=${OTEL_SERVICE_NAME:-}"
    "CHAT_TRACE_EXPORTER=${CHAT_TRACE_EXPORTER:-}"
    "MOBIUS_TASK_MANAGER_PROMOTION=${MOBIUS_TASK_MANAGER_PROMOTION}"
    "CHAT_CORS_ORIGINS=${CHAT_CORS_ORIGINS}"
    "MOBIUS_OS_AUTH_URL=${MOBIUS_OS_AUTH_URL}"
    "CHAT_AUTH_MODE=${CHAT_AUTH_MODE:-}"
    # Google sign-in (web). Surfaced to the frontend via /api/v1/public-config.
    "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}"
    "MOBIUS_REACT_CRITIC=${MOBIUS_REACT_CRITIC:-}"
    "MOBIUS_REACT_CORPUS_CONFIDENCE_MIN=${MOBIUS_REACT_CORPUS_CONFIDENCE_MIN:-}"
    "CHAT_HIPAA_MODE=${CHAT_HIPAA_MODE}"
    # Secret Manager loader needs this to know which project to fetch
    # secrets from. Cloud Run sets GOOGLE_CLOUD_PROJECT automatically,
    # but CHAT_GCP_PROJECT wins if set — useful during debugging.
    "CHAT_GCP_PROJECT=${GCP_PROJECT}"
)

# Secrets → Cloud Run mounts each as an env var. ``name:latest`` pins
# to whatever the current secret version is at deploy time (re-runs
# after a rotation pick up the new version automatically).
SET_SECRETS=(
    "GROQ_API_KEY=groq-api-key:latest"
    "ANTHROPIC_API_KEY=anthropic-api-key:latest"
    "JWT_SECRET=jwt-secret:latest"
    # Beta: postgres superuser password for direct Cloud SQL connect.
    # Injected into CHAT_RAG_DATABASE_URL at connect time by db_client.
    "CHAT_DB_PASSWORD=db-password:latest"
    "CHROMA_AUTH_TOKEN=chroma-auth-token:latest"
    # Shared secret that gates /internal/skill-llm. Sibling services
    # (mobius-rag, mobius-qa/lexicon-maintenance) POST with
    # ``X-Mobius-Skill-LLM-Key: <this>`` and chat rejects on mismatch.
    # Must stay in the deploy script — out-of-band ``gcloud run
    # services update --update-secrets ...`` patches were lost on
    # every redeploy before 2026-04-23 because --set-secrets in this
    # script REPLACES the whole secret set. Keep this line.
    "MOBIUS_SKILL_LLM_INTERNAL_KEY=mobius-skill-llm-internal-key:latest"
    # Curator tools (Phase 13.5, 2026-04-26): admin-write auth for
    # lookup_authoritative_sources + ingest_url calls into mobius-rag.
    # Read by app/pipeline/curator_tools.py (also accepts ADMIN_API_KEY
    # as a fallback for older callers). Same secret mobius-rag mints
    # for its own admin endpoints.
    "MOBIUS_RAG_ADMIN_KEY=rag-admin-api-key:latest"
)

# ── Deploy ──────────────────────────────────────────────────────────

echo "── Deploying ${SERVICE_NAME} to ${GCP_PROJECT}/${GCP_REGION} ──"
run gcloud run deploy "${SERVICE_NAME}" \
    --project="${GCP_PROJECT}" \
    --region="${GCP_REGION}" \
    --image="${IMAGE_TAG}" \
    --service-account="${SERVICE_ACCOUNT}" \
    --platform=managed \
    --allow-unauthenticated \
    --memory="${RUN_MEMORY}" \
    --cpu="${RUN_CPU}" \
    --concurrency="${RUN_CONCURRENCY}" \
    --timeout="${RUN_TIMEOUT}" \
    --min-instances="${RUN_MIN_INSTANCES}" \
    --max-instances="${RUN_MAX_INSTANCES}" \
    --port=8080 \
    --add-cloudsql-instances="${CLOUDSQL_INSTANCE}" \
    ${RUN_VPC_CONNECTOR:+--vpc-connector="${RUN_VPC_CONNECTOR}"} \
    ${RUN_VPC_EGRESS:+--vpc-egress="${RUN_VPC_EGRESS}"} \
    --set-env-vars="$(csv_env "${SET_ENV_VARS[@]}")" \
    --set-secrets="$(csv_env "${SET_SECRETS[@]}")" \
    --cpu-boost \
    --no-cpu-throttling \
    --execution-environment=gen2

# ── Post-deploy: populate CHAT_CORS_ORIGINS with the real URL ───────
# First deploy → the Cloud Run URL isn't known until the service is
# created. If the env file left CHAT_CORS_ORIGINS empty, update the
# service with the real URL now. Subsequent deploys skip this because
# CHAT_CORS_ORIGINS is already set.

if [[ -z "${CHAT_CORS_ORIGINS:-}" && "${DRY_RUN}" -eq 0 ]]; then
    SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
        --project="${GCP_PROJECT}" \
        --region="${GCP_REGION}" \
        --format='value(status.url)')"
    echo "── Updating CHAT_CORS_ORIGINS=${SERVICE_URL} ──"
    run gcloud run services update "${SERVICE_NAME}" \
        --project="${GCP_PROJECT}" \
        --region="${GCP_REGION}" \
        --update-env-vars="CHAT_CORS_ORIGINS=${SERVICE_URL}"
    echo
    echo "Persist this in deploy/${ENV_LABEL}.env for future deploys:"
    echo "  CHAT_CORS_ORIGINS=${SERVICE_URL}"
fi

echo
echo "✓ Deploy complete: ${IMAGE_TAG}"
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
    --project="${GCP_PROJECT}" --region="${GCP_REGION}" \
    --format='value(status.url)' 2>/dev/null || echo '')"
if [[ -n "${SERVICE_URL}" ]]; then
    echo "  Service URL: ${SERVICE_URL}"
else
    echo "  Service URL: (not queryable in dry-run)"
fi
echo

# ── Post-deploy smoke ───────────────────────────────────────────────
# Runs a handful of critical-path probes against the just-deployed
# revision and fails the deploy when any of them comes back wrong.
# Catches the class of bug that sneaks past unit tests:
#   * Env-var name drift (INSTANT_RAG_URL vs CHAT_SKILLS_INSTANT_RAG_URL)
#   * Missing transitive deps (python-multipart)
#   * Downstream service URL misconfig
#   * Startup-ordering regressions
#
# --skip-smoke bypasses this (emergency only — prefer rolling back).
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "(dry-run: skipping post-deploy smoke)"
elif [[ "${SKIP_SMOKE}" -eq 1 ]]; then
    echo "⚠ --skip-smoke: post-deploy smoke bypassed. You should not rely on this."
    echo "  To validate the deploy manually:"
    echo "    scripts/post_deploy_smoke.sh ${SERVICE_URL}"
elif [[ -z "${SERVICE_URL}" ]]; then
    echo "⚠ Could not resolve service URL; skipping post-deploy smoke."
else
    echo "── Running post-deploy smoke ──"
    if "${SCRIPT_DIR}/post_deploy_smoke.sh" "${SERVICE_URL}"; then
        :  # pass — smoke script prints its own summary
    else
        echo
        echo "✗ Post-deploy smoke failed. The revision is serving traffic but" >&2
        echo "  one or more critical paths are broken. Review the probe output" >&2
        echo "  above, then either fix-forward or roll back:" >&2
        echo "    gcloud run services update-traffic ${SERVICE_NAME} \\" >&2
        echo "      --project=${GCP_PROJECT} --region=${GCP_REGION} \\" >&2
        echo "      --to-revisions=<PREVIOUS_REVISION>=100" >&2
        exit 72
    fi
fi
