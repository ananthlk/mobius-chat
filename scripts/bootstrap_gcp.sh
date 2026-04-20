#!/usr/bin/env bash
#
# One-time GCP project setup for mobius-chat.
#
# Idempotent: every step checks current state before doing anything,
# so re-running against an already-bootstrapped project is a no-op.
# Safe to run when you're not sure if a step ran before.
#
# What it creates (or verifies)
# -----------------------------
#   1. GCP APIs enabled (run, secretmanager, sqladmin, artifactregistry,
#      cloudbuild, vpcaccess, iamcredentials)
#   2. Artifact Registry repo ``mobius-chat`` in us-central1
#   3. Cloud SQL IAM database user for the service account
#   4. Service-account IAM roles needed at runtime:
#        roles/cloudsql.client
#        roles/cloudsql.instanceUser
#        roles/aiplatform.user
#        roles/secretmanager.secretAccessor  (already set on secrets)
#        roles/logging.logWriter
#        roles/monitoring.metricWriter
#   5. Cloud Build service account permissions to push to Artifact
#      Registry + write Cloud Run revisions.
#
# What it does NOT do
# -------------------
# * Create Cloud SQL instance. We assume mobius-platform-dev-db
#   already exists (discovered via ``gcloud sql instances list``).
#   Creating a new instance is a separate, expensive decision.
# * Create secrets. Done separately via ``gcloud secrets create``
#   with real key material; scripts don't touch those.
# * Deploy the service. That's scripts/deploy.sh.
#
# Prereqs
# -------
#   * gcloud CLI, authenticated as a user with Owner or equivalent on
#     the target project
#   * gcloud config set project <...> OR pass the env file

set -euo pipefail

ENV_LABEL="${1:-}"
if [[ -z "${ENV_LABEL}" ]]; then
    echo "usage: $0 <env>   # e.g. dev" >&2
    exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${CHAT_DIR}/deploy/${ENV_LABEL}.env"
[[ -f "${ENV_FILE}" ]] || { echo "${ENV_FILE} not found" >&2; exit 66; }

set -a; source "${ENV_FILE}"; set +a

# ── 1. APIs ──────────────────────────────────────────────────────────

echo "── Enabling required APIs on ${GCP_PROJECT} ──"
REQUIRED_APIS=(
    run.googleapis.com
    secretmanager.googleapis.com
    sqladmin.googleapis.com
    artifactregistry.googleapis.com
    cloudbuild.googleapis.com
    vpcaccess.googleapis.com
    iamcredentials.googleapis.com
    aiplatform.googleapis.com
    logging.googleapis.com
    monitoring.googleapis.com
)
# gcloud services enable is idempotent; pass them all at once for speed.
gcloud services enable "${REQUIRED_APIS[@]}" --project="${GCP_PROJECT}"

# ── 2. Artifact Registry repo ────────────────────────────────────────

echo "── Ensuring Artifact Registry repo '${AR_REPO}' exists ──"
if gcloud artifacts repositories describe "${AR_REPO}" \
        --project="${GCP_PROJECT}" \
        --location="${GCP_REGION}" >/dev/null 2>&1; then
    echo "  repo exists: ${AR_REPO}"
else
    gcloud artifacts repositories create "${AR_REPO}" \
        --project="${GCP_PROJECT}" \
        --location="${GCP_REGION}" \
        --repository-format=docker \
        --description="Container images for mobius-chat Cloud Run deploys"
fi

# ── 3. Cloud SQL IAM user ────────────────────────────────────────────
#
# Cloud SQL IAM auth works by having the Postgres user name equal the
# service-account email. No password is stored on the DB side; the
# Cloud SQL Proxy exchanges the SA's Google-issued IAM token for a
# short-lived DB credential.

echo "── Ensuring Cloud SQL IAM user '${CLOUDSQL_IAM_USER}' exists ──"
if gcloud sql users list --instance="${CLOUDSQL_INSTANCE##*:}" \
        --project="${GCP_PROJECT}" \
        --format='value(name)' | grep -qxF "${CLOUDSQL_IAM_USER}"; then
    echo "  user exists: ${CLOUDSQL_IAM_USER}"
else
    gcloud sql users create "${CLOUDSQL_IAM_USER}" \
        --instance="${CLOUDSQL_INSTANCE##*:}" \
        --project="${GCP_PROJECT}" \
        --type=cloud_iam_service_account
fi

# Grant the IAM user access to the chat database. Cloud SQL creates
# the PG role automatically via IAM auth, but it has no privileges on
# existing DBs until we grant them. Run once; subsequent runs are
# idempotent (GRANT ... is safe to re-issue).
#
# We use gcloud sql connect rather than spawning cloud-sql-proxy to
# keep this script self-contained (no extra binary dep).

echo "── Granting ${CLOUDSQL_IAM_USER} privileges on ${CLOUDSQL_DATABASE} ──"
#
# gcloud sql connect doesn't support non-interactive auth cleanly
# (it prompts for DB password for the ``postgres`` user). We print
# the grant commands instead and let the operator paste them once,
# from a terminal that can handle the password prompt.

GRANT_SQL="$(cat <<SQL
GRANT ALL PRIVILEGES ON DATABASE ${CLOUDSQL_DATABASE} TO "${CLOUDSQL_IAM_USER}";
GRANT ALL ON SCHEMA public TO "${CLOUDSQL_IAM_USER}";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "${CLOUDSQL_IAM_USER}";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO "${CLOUDSQL_IAM_USER}";
SQL
)"

echo "  Run these SQL statements once (from a terminal that can accept"
echo "  the postgres-user password prompt):"
echo
echo "    gcloud sql connect ${CLOUDSQL_INSTANCE##*:} \\"
echo "      --project=${GCP_PROJECT} --user=postgres --database=${CLOUDSQL_DATABASE}"
echo
printf '%s\n' "${GRANT_SQL}" | sed 's/^/    /'
echo
echo "  (Safe to skip if you already granted these on a previous run.)"

# ── 4. Service-account project roles ─────────────────────────────────
# Secret Manager scoped bindings already exist (done when secrets
# were created). These are the project-wide roles the runtime needs.

echo "── Granting project roles to ${SERVICE_ACCOUNT} ──"
REQUIRED_ROLES=(
    roles/cloudsql.client
    roles/cloudsql.instanceUser
    roles/aiplatform.user
    roles/logging.logWriter
    roles/monitoring.metricWriter
)
for role in "${REQUIRED_ROLES[@]}"; do
    # ``--condition=None`` stops gcloud from prompting about the
    # (deprecated) no-condition behavior.
    gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
        --member="serviceAccount:${SERVICE_ACCOUNT}" \
        --role="${role}" \
        --condition=None \
        --quiet >/dev/null
    echo "  ✓ ${role}"
done

# ── 5. Cloud Build SA → deploy targets ──────────────────────────────
# The Cloud Build default SA needs to:
#   * push images to Artifact Registry (roles/artifactregistry.writer)
#   * deploy Cloud Run services (roles/run.admin)
#   * impersonate the runtime SA so ``--service-account`` works
#     (roles/iam.serviceAccountUser on the runtime SA)

CLOUDBUILD_SA="$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')@cloudbuild.gserviceaccount.com"
echo "── Granting Cloud Build SA (${CLOUDBUILD_SA}) deploy roles ──"

for role in roles/artifactregistry.writer roles/run.admin; do
    gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
        --member="serviceAccount:${CLOUDBUILD_SA}" \
        --role="${role}" \
        --condition=None --quiet >/dev/null
    echo "  ✓ ${role}"
done

# actAs: let Cloud Build deploy as the runtime SA.
gcloud iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
    --project="${GCP_PROJECT}" \
    --member="serviceAccount:${CLOUDBUILD_SA}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet >/dev/null
echo "  ✓ roles/iam.serviceAccountUser (on ${SERVICE_ACCOUNT})"

echo
echo "✓ Bootstrap complete for ${GCP_PROJECT} / ${ENV_LABEL}"
echo
echo "Next steps:"
echo "  1. Apply schema migrations:   scripts/migrate.sh ${ENV_LABEL}"
echo "  2. First deploy:              scripts/deploy.sh ${ENV_LABEL}"
