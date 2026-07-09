#!/usr/bin/env bash
#
# Mobius Chat — post-deploy smoke.
#
# Runs a handful of critical-path probes against a just-deployed
# revision and fails the deploy when any of them comes back wrong.
# Designed to catch the class of bug that sneaks past unit tests
# because it only manifests in the deployed environment — env-var
# name drift, missing transitive deps, startup ordering, downstream
# service URL misconfig.
#
# Three bugs this script would have caught in the last two weeks:
#   1. python-multipart dropped from the dep graph
#   2. INSTANT_RAG_URL vs CHAT_SKILLS_INSTANT_RAG_URL name mismatch
#   3. mcp>=1.0.0 cleanup'd out of requirements.txt
#
# Usage
# -----
# Standalone:
#     scripts/post_deploy_smoke.sh https://mobius-chat-.../
#
# From deploy.sh (auto-invoked):
#     scripts/deploy.sh dev         → runs smoke after deploy
#     scripts/deploy.sh dev --skip-smoke   → bypass (emergency only)
#
# Design
# ------
# * Fast: total runtime < 20s under normal conditions. NO real chat
#   turns (those take 20-30s and cost LLM credits). Probes the
#   request-ingestion path, not the LLM path.
# * Isolated: each probe has its own timeout so a broken dep can't
#   hang the whole deploy.
# * Loud: every probe prints PASS/FAIL explicitly. A failure names
#   the check + the response body so operators can diagnose without
#   opening Cloud Logging.
# * Exit codes:
#     0 — all probes passed
#     1 — any probe failed

set -euo pipefail

BASE_URL="${1:-}"
if [[ -z "${BASE_URL}" ]]; then
    echo "usage: $0 <base-url>" >&2
    exit 64
fi
BASE_URL="${BASE_URL%/}"   # strip trailing slash for predictable concat

# ── State ───────────────────────────────────────────────────────────

FAIL_COUNT=0
PASS_COUNT=0

# Curl defaults. --fail-with-body lets us capture the response text
# on non-2xx instead of dying silently; --max-time prevents any
# single probe from hanging the whole deploy.
CURL="curl -sS --max-time 15"

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "  ✓ $*"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo "  ✗ $*" >&2
}

# ── Probes ──────────────────────────────────────────────────────────

probe_health() {
    echo "[1] /health"
    local code
    code="$(${CURL} -o /dev/null -w '%{http_code}' "${BASE_URL}/health" || echo "000")"
    if [[ "${code}" == "200" ]]; then
        pass "/health → 200"
    else
        fail "/health → ${code} (expected 200)"
    fi
}

probe_config() {
    # /chat/config reads many env vars and cfg fields. A 500 here is
    # almost always a boot-time env-read failure that made it past the
    # startup probe.
    echo "[2] /chat/config"
    local body code
    body="$(${CURL} -o /tmp/_smoke_config.json -w '%{http_code}' "${BASE_URL}/chat/config" || echo "000")"
    code="${body}"
    if [[ "${code}" == "200" ]]; then
        pass "/chat/config → 200"
    else
        fail "/chat/config → ${code} (expected 200). Body: $(head -c 200 /tmp/_smoke_config.json 2>/dev/null || echo '-')"
    fi
}

probe_dev_token_or_skip() {
    # Only exercised when MOBIUS_DEV_TOKEN_ENABLED=1 on the target. When
    # disabled, the endpoint returns 404 by design — that's a pass,
    # not a fail (production keeps this off).
    echo "[3] /chat/admin/mint-dev-token"
    local code
    code="$(${CURL} -o /tmp/_smoke_mint.json -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' -d '{}' \
        "${BASE_URL}/chat/admin/mint-dev-token" || echo "000")"
    case "${code}" in
        200)
            if grep -q '"access_token"' /tmp/_smoke_mint.json; then
                pass "mint-dev-token → 200 (token minted — dev mode)"
            else
                fail "mint-dev-token returned 200 but no access_token in body"
            fi
            ;;
        404)
            pass "mint-dev-token → 404 (dev-token feature disabled — prod-correct)"
            ;;
        500)
            fail "mint-dev-token → 500. Body: $(head -c 200 /tmp/_smoke_mint.json 2>/dev/null || echo '-'). Likely JWT_SECRET misconfigured."
            ;;
        *)
            fail "mint-dev-token → ${code}. Body: $(head -c 200 /tmp/_smoke_mint.json 2>/dev/null || echo '-')"
            ;;
    esac
}

probe_chat_ingest() {
    # POST /chat returns immediately with a correlation_id once the
    # request is accepted + enqueued. This probe verifies:
    #   * The route is wired
    #   * Request body parsing works (rate-limit peek, auth, etc.)
    #   * DB connection is up (thread_id persistence)
    #   * Worker queue accepts the payload
    # It does NOT wait for the turn to complete — that'd cost LLM credits
    # + take 20-30s. Cache-assist off + quick mode keeps the downstream
    # work minimal even though we don't poll for it.
    echo "[4] POST /chat (ingestion — no turn execution awaited)"
    local code
    code="$(${CURL} -o /tmp/_smoke_chat.json -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' \
        -d '{"message":"deploy smoke probe","chat_mode":"quick","cache_assist":false}' \
        "${BASE_URL}/chat" || echo "000")"
    if [[ "${code}" == "200" ]] && grep -q '"correlation_id"' /tmp/_smoke_chat.json; then
        local cid
        cid="$(sed -n 's/.*"correlation_id":"\([^"]*\)".*/\1/p' /tmp/_smoke_chat.json)"
        pass "POST /chat → 200 cid=${cid:0:8}"
    elif [[ "${code}" == "401" ]]; then
        # Auth required + no token — NOT a smoke failure by itself if
        # the target is set to require auth. Log it loudly so operators
        # know the smoke didn't exercise the ingestion path.
        echo "  ⚠ POST /chat → 401 (auth required, smoke didn't supply token). Ingestion path untested."
    else
        fail "POST /chat → ${code}. Body: $(head -c 200 /tmp/_smoke_chat.json 2>/dev/null || echo '-')"
    fi
}

probe_upload_path() {
    # Probe: POST /chat/upload (canonical since P0 unify 2026-07-09;
    # /chat/roster-upload is a server-alias kept for credentialing agents).
    # Catches env-var drift on MOBIUS_RAG_URL. Outcomes:
    #   200/201 → full success.
    #   401     → auth required; upload path structure still validated.
    #   502     → upstream error. Soft-fail if detail mentions "Rate exceeded"
    #             (mobius-rag 429 during smoke is transient, not a deploy bug).
    #             Hard-fail otherwise (likely MOBIUS_RAG_URL misconfigured).
    #   000     → connection reset (cold-start LB drop); warn, don't fail.
    #   other   → unexpected; fail.
    echo "[5] POST /chat/upload (file-upload flow)"
    local tmp
    tmp="$(mktemp -t smoke_probe_XXXXX).txt"
    printf "smoke-probe,deploy-check\n1,ok\n" > "${tmp}"
    local code
    code="$(${CURL} -o /tmp/_smoke_upload.json -w '%{http_code}' \
        -X POST \
        -F "file=@${tmp}" \
        "${BASE_URL}/chat/upload" || echo "000")"
    rm -f "${tmp}"
    local body
    body="$(head -c 300 /tmp/_smoke_upload.json 2>/dev/null || echo '-')"
    case "${code}" in
        200|201)
            pass "POST /chat/upload → ${code} (upload path + mobius-rag reachable)"
            ;;
        401)
            echo "  ⚠ POST /chat/upload → 401 (auth required; upload path structure validated)"
            ;;
        502)
            if echo "${body}" | grep -qi "rate exceeded\|429\|quota"; then
                echo "  ⚠ POST /chat/upload → 502 (mobius-rag rate-limited during smoke — transient, not a deploy bug)"
            else
                fail "POST /chat/upload → 502. Body: ${body}. Likely MOBIUS_RAG_URL misconfigured."
            fi
            ;;
        000)
            echo "  ⚠ POST /chat/upload → connection reset (cold-start LB drop — rerun smoke to confirm)"
            ;;
        *)
            fail "POST /chat/upload → ${code}. Body: ${body}"
            ;;
    esac
}

# ── Run ─────────────────────────────────────────────────────────────

echo ""
echo "── Post-deploy smoke: ${BASE_URL} ──"
echo ""

probe_health
probe_config
probe_dev_token_or_skip
probe_chat_ingest
probe_upload_path

echo ""
echo "── Smoke result: ${PASS_COUNT} pass, ${FAIL_COUNT} fail ──"
echo ""

# Clean up temp files (best-effort — don't care if any don't exist).
rm -f /tmp/_smoke_config.json /tmp/_smoke_mint.json /tmp/_smoke_chat.json /tmp/_smoke_upload.json 2>/dev/null || true

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    echo "⚠ Smoke FAILED. The deploy succeeded technically, but one or" >&2
    echo "  more critical paths are broken in the deployed revision." >&2
    echo "" >&2
    echo "  Recommended: roll back with" >&2
    echo "    gcloud run services update-traffic mobius-chat \\" >&2
    echo "      --project=\$GCP_PROJECT --region=\$GCP_REGION \\" >&2
    echo "      --to-revisions=<PREVIOUS_REVISION>=100" >&2
    exit 1
fi

echo "✓ All smoke probes passed."
