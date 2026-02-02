#!/usr/bin/env bash
# Simulate: RAG (approved docs) → publish → datalake → datalake moves to our environment.
# 1. Start our DB (target env)
# 2. Create a "source" DB (simulates RAG/datalake side) and seed it with approved docs
# 3. Run copy_from_rag: source → target (simulates datalake moving published docs to our env)
set -e
cd "$(dirname "$0")/.."
SCRIPT_DIR="$(pwd)"

# Prerequisites: Docker Desktop (Mac), Python 3 + venv + deps
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found. Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/"
  echo "  or: brew install --cask docker"
  echo "Start Docker Desktop, then run this script again."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose not available. Use Docker Desktop (includes Compose)."
  exit 1
fi

[ -d .venv ] && source .venv/bin/activate
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

echo "[simulate] 1. Starting RAG DB (target environment)..."
docker compose up -d

echo "[simulate] 2. Waiting for Postgres..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if docker compose exec -T ragdb pg_isready -U mobius -d mobius_chat_rag >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T ragdb pg_isready -U mobius -d mobius_chat_rag || { echo "Postgres not ready"; exit 1; }

echo "[simulate] 3. Creating source DB (simulates RAG/datalake with approved docs)..."
docker compose exec -T ragdb psql -U mobius -d postgres -c "CREATE DATABASE rag_source;" 2>/dev/null || true

echo "[simulate] 4. Applying schema to source DB..."
cat "$SCRIPT_DIR/db/schema/001_rag_schema.sql" | docker compose exec -T ragdb psql -U mobius -d rag_source -f - >/dev/null 2>&1 || true

echo "[simulate] 5. Seeding source DB (simulates RAG approved docs ready to publish)..."
export RAG_DATABASE_URL="postgresql://mobius:mobius@localhost:5433/rag_source"
# Python seed loads .env via dotenv; do not source .env here (paths with spaces break when sourced)
$PYTHON -m app.db.seed || { echo "Seed failed (need Vertex creds in .env? Quote paths with spaces.). Continuing anyway."; }

echo "[simulate] 6. Running copy: source → target..."
# Use RAG_SOURCE_DATABASE_URL from .env if set (real RAG DB with many chunks); else use local rag_source
if [ -f .env ]; then
  _src=$(grep -E '^RAG_SOURCE_DATABASE_URL=' .env 2>/dev/null | sed 's/^RAG_SOURCE_DATABASE_URL=//' | sed 's/^["'\'']//;s/["'\'']$//' | tr -d '\r')
  if [ -n "$_src" ]; then
    export RAG_SOURCE_DATABASE_URL="$_src"
    echo "[simulate] Using source from .env (real RAG DB)"
  fi
fi
if [ -z "$RAG_SOURCE_DATABASE_URL" ]; then
  export RAG_SOURCE_DATABASE_URL="postgresql://mobius:mobius@localhost:5433/rag_source"
  echo "[simulate] Using local rag_source (simulation)"
fi
export RAG_DATABASE_URL="postgresql://mobius:mobius@localhost:5433/mobius_chat_rag"
$PYTHON -m app.db.copy_from_rag

echo "[simulate] Done. Our environment (mobius_chat_rag) now has the published docs."
