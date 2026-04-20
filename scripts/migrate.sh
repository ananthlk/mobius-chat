#!/usr/bin/env bash
#
# Apply db/schema/*.sql migrations to the chat Cloud SQL database.
#
# Idempotent + ordered: each applied migration is recorded in
# ``schema_migrations`` (created on first run) so reruns skip already-
# applied files. Files are applied in lexical order, which matches
# chat's NNN_*.sql naming convention.
#
# Usage:
#     scripts/migrate.sh <env>
#     scripts/migrate.sh <env> --dry-run      # list pending, don't apply
#     scripts/migrate.sh <env> --file 020_llm_analytics.sql  # apply just one
#
# Connection
# ----------
# Runs the Cloud SQL Auth Proxy in the background and points psql at
# its local socket. This means the operator needs:
#   * ``gcloud auth application-default login`` (Cloud SQL IAM auth)
#   * the cloud-sql-proxy binary on PATH (``brew install cloud-sql-proxy``)
#   * the IAM role ``roles/cloudsql.client`` on their user
#   * the IAM role ``roles/cloudsql.instanceUser`` on the DB user
#
# Safety
# ------
# * Migrations run inside a single transaction per file. A syntax
#   error in file N doesn't half-apply it.
# * The schema_migrations ledger write happens in the same transaction
#   as the migration, so a committed file is always tracked.
# * Migrations are never re-ordered. ``NNN_foo.sql`` landing after
#   ``MMM_bar.sql`` applies in lexical order.

set -euo pipefail

ENV_LABEL="${1:-}"
if [[ -z "${ENV_LABEL}" ]]; then
    echo "usage: $0 <env> [--dry-run] [--file NAME.sql]" >&2
    exit 64
fi
shift

DRY_RUN=0
SINGLE_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --file)    SINGLE_FILE="$2"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 64 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHAT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SCHEMA_DIR="${CHAT_DIR}/db/schema"
ENV_FILE="${CHAT_DIR}/deploy/${ENV_LABEL}.env"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "error: ${ENV_FILE} not found" >&2
    exit 66
fi

set -a; source "${ENV_FILE}"; set +a

for required in GCP_PROJECT CLOUDSQL_INSTANCE CLOUDSQL_DATABASE CLOUDSQL_IAM_USER; do
    if [[ -z "${!required:-}" ]]; then
        echo "error: ${required} missing from ${ENV_FILE}" >&2
        exit 65
    fi
done

# ── Cloud SQL proxy ─────────────────────────────────────────────────
# Use a per-invocation Unix socket under $TMPDIR so parallel deploys
# (e.g. dev + prod) don't collide on the default /cloudsql path.

PROXY_SOCKET_DIR="$(mktemp -d -t chat-migrate-XXXXXX)"
trap 'rm -rf "${PROXY_SOCKET_DIR}"; jobs -p | xargs -r kill 2>/dev/null || true' EXIT

if ! command -v cloud-sql-proxy >/dev/null; then
    echo "error: cloud-sql-proxy not on PATH. Install: brew install cloud-sql-proxy" >&2
    exit 69
fi

echo "── Starting Cloud SQL Auth Proxy ──"
cloud-sql-proxy \
    --unix-socket "${PROXY_SOCKET_DIR}" \
    --auto-iam-authn \
    "${CLOUDSQL_INSTANCE}" &
PROXY_PID=$!

# Give the proxy a moment to bind the socket. Two-second poll is
# plenty; we bail if it hasn't materialized after ~10s.
for i in 1 2 3 4 5; do
    if [[ -S "${PROXY_SOCKET_DIR}/${CLOUDSQL_INSTANCE}/.s.PGSQL.5432" ]]; then
        break
    fi
    sleep 2
done
if [[ ! -S "${PROXY_SOCKET_DIR}/${CLOUDSQL_INSTANCE}/.s.PGSQL.5432" ]]; then
    echo "error: Cloud SQL Proxy failed to bind socket. Check gcloud auth." >&2
    exit 74
fi

PSQL_DSN="host=${PROXY_SOCKET_DIR}/${CLOUDSQL_INSTANCE} dbname=${CLOUDSQL_DATABASE} user=${CLOUDSQL_IAM_USER}"

psql_exec() {
    PGOPTIONS='-c client_min_messages=warning' psql "${PSQL_DSN}" -v ON_ERROR_STOP=1 "$@"
}

# ── Ensure the ledger table exists ──────────────────────────────────

psql_exec -c "
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT NOT NULL
);
" >/dev/null

# ── Discover pending migrations ─────────────────────────────────────

mapfile -t ALL_FILES < <(find "${SCHEMA_DIR}" -maxdepth 1 -type f -name '*.sql' -printf '%f\n' | sort)

if [[ -n "${SINGLE_FILE}" ]]; then
    ALL_FILES=("${SINGLE_FILE}")
fi

APPLIED=$(psql_exec -At -c "SELECT filename FROM schema_migrations" | sort)

PENDING=()
for f in "${ALL_FILES[@]}"; do
    if ! grep -qxF "${f}" <<< "${APPLIED}"; then
        PENDING+=("${f}")
    fi
done

if [[ ${#PENDING[@]} -eq 0 ]]; then
    echo "✓ No pending migrations. Database schema up to date."
    exit 0
fi

echo "── ${#PENDING[@]} pending migration(s) ──"
printf '  %s\n' "${PENDING[@]}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo
    echo "(dry-run; nothing applied)"
    exit 0
fi

# ── Apply ───────────────────────────────────────────────────────────
# Each file runs in its own transaction. A file that references
# objects created by an earlier file in the same batch is fine because
# we commit between files.

for f in "${PENDING[@]}"; do
    path="${SCHEMA_DIR}/${f}"
    if [[ ! -f "${path}" ]]; then
        echo "skip: ${f} (not found on disk; may have been renamed)"
        continue
    fi
    checksum="$(shasum -a 256 "${path}" | cut -d' ' -f1)"
    echo "── Applying ${f} ──"

    psql_exec <<SQL
BEGIN;
\i ${path}
INSERT INTO schema_migrations (filename, checksum) VALUES ('${f}', '${checksum}');
COMMIT;
SQL
done

echo
echo "✓ Applied ${#PENDING[@]} migration(s) to ${CLOUDSQL_DATABASE}"
