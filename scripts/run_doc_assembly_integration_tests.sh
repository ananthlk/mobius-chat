#!/usr/bin/env bash
# Run doc assembly integration tests (DB + optional Google).
#
# Requires: CHAT_RAG_DATABASE_URL in .env (mobius-chat or Mobius root)
# Optional: CHAT_SKILLS_GOOGLE_SEARCH_URL for Google search tests
#   - If unset and mobius-skills/google-search exists, starts it on port 8004 and sets URL
#
# Usage:
#   ./scripts/run_doc_assembly_integration_tests.sh
#   CHAT_SKILLS_GOOGLE_SEARCH_URL=https://your-skills-api/search? ./scripts/run_doc_assembly_integration_tests.sh

set -e
# From mobius-chat/scripts/ go to Mobius root
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Load .env from mobius-chat
if [ -f mobius-chat/.env ]; then
  set -a
  source mobius-chat/.env
  set +a
fi
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

echo "CHAT_RAG_DATABASE_URL: ${CHAT_RAG_DATABASE_URL:-(not set)}"
echo "CHAT_SKILLS_GOOGLE_SEARCH_URL: ${CHAT_SKILLS_GOOGLE_SEARCH_URL:-(not set)}"

# If Google URL not set, start mobius-skills/google-search on 8004 for Google tests
GOOGLE_PID=""
if [[ -z "${CHAT_SKILLS_GOOGLE_SEARCH_URL:-}" ]] && [[ -d mobius-skills/google-search ]]; then
  echo "Starting mobius-google-search on 8004 for Google tests..."
  (cd mobius-skills/google-search && "$ROOT/.venv/bin/python3" -m uvicorn app.main:app --host 127.0.0.1 --port 8004) 2>/dev/null &
  GOOGLE_PID=$!
  sleep 2
  export CHAT_SKILLS_GOOGLE_SEARCH_URL="http://127.0.0.1:8004/search?"
  echo "CHAT_SKILLS_GOOGLE_SEARCH_URL set to $CHAT_SKILLS_GOOGLE_SEARCH_URL"
fi
echo ""

.venv/bin/python -m pytest mobius-chat/tests/test_doc_assembly_integration.py -v -s "$@"
EXIT=$?

[[ -n "$GOOGLE_PID" ]] && kill "$GOOGLE_PID" 2>/dev/null || true
exit "$EXIT"
