# syntax=docker/dockerfile:1.7
#
# Mobius Chat — Cloud Run image (clean-slate rewrite, 2026-04-20).
#
# ─────────────────────────────────────────────────────────────────────
# Design notes (why this Dockerfile looks the way it does)
# ─────────────────────────────────────────────────────────────────────
#
# * **Monolith for beta.** Chat's API and worker run in the same
#   container via the in-memory queue path (``CHAT_QUEUE_TYPE=memory``).
#   FastAPI's startup hook kicks off ``start_worker_background`` so a
#   single ``uvicorn`` process serves HTTP and consumes queued turns.
#   When we outgrow that we split into two services; today it's 1.
#
# * **Multi-stage build** with a fat builder and slim runtime. Final
#   image is ~250 MB vs ~1.1 GB if we kept build deps (build-essential,
#   libffi-dev, pip cache, git history for editable installs).
#
# * **Sibling packages vendored at build time.** ``mobius-contracts``,
#   ``mobius-skills-core``, and ``mobius-retriever`` are imported by
#   chat but live in sibling repos and are installed via ``pip install
#   -e ../foo`` in dev. Cloud Run containers can't follow relative
#   paths, so the build context is the Mobius parent directory and we
#   ``COPY`` each sibling into /build/. The build context is
#   controlled from scripts/deploy.sh to avoid bloating the image with
#   unrelated siblings.
#
# * **Non-root runtime.** ``chat`` user owns /app; Cloud Run doesn't
#   require non-root but it's cheap defense in depth against a future
#   container-escape CVE in Python or a dep.
#
# * **Python 3.13** matches local dev + skills-core + db-agent. Slim
#   Debian base is supported upstream.
#
# * **``gcloud`` intentionally absent.** Chat talks to Secret Manager
#   via the Python SDK, not the CLI — keeps the image small and avoids
#   the metadata-server auth quirks that bit earlier container attempts.
#
# ─────────────────────────────────────────────────────────────────────
# Build context expectations
# ─────────────────────────────────────────────────────────────────────
#
# Build must run from /Users/ananth/Mobius (the parent directory), so
# that all sibling repos are visible to ``COPY``:
#
#   docker build -f mobius-chat/Dockerfile -t chat .
#
# ``scripts/deploy.sh`` sets this up. Running ``docker build`` from
# inside mobius-chat/ will fail because siblings aren't reachable.


# ══════════════════════════════════════════════════════════════════════
# Stage 1 — builder
# ══════════════════════════════════════════════════════════════════════
#
# Compile-time dependencies only; produces a ready-to-copy /venv.

FROM python:3.13-slim AS builder

# ``build-essential`` + ``libffi-dev`` are needed to compile
# cryptography's native bindings and any wheel that lacks a manylinux
# prebuilt for 3.13. They live only in this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Dedicated venv keeps the interpreter and site-packages as one unit
# we can copy wholesale into the runtime stage.
ENV VIRTUAL_ENV=/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Order: chat's third-party deps first (cacheable layer), then the
# three vendored sibling packages, then the chat source itself.
# Each layer above invalidates only when its own inputs change, so
# subsequent builds during a normal code change only re-copy the
# chat/ source.

WORKDIR /build

# --- Third-party deps ---
# requirements.txt is the sole source of truth; pinned ranges only.
# See deploy/README.md for dep audit guidance.
COPY mobius-chat/requirements.txt /build/mobius-chat/requirements.txt
RUN pip install --no-cache-dir -r /build/mobius-chat/requirements.txt

# --- Sibling packages (vendored from parent build context) ---
# These install as regular packages (NOT editable) so the venv is
# self-contained and nothing in /app depends on /build at runtime.
COPY mobius-contracts/     /build/mobius-contracts/
COPY mobius-skills-core/   /build/mobius-skills-core/
COPY mobius-retriever/     /build/mobius-retriever/
RUN pip install --no-cache-dir \
        /build/mobius-contracts \
        /build/mobius-skills-core \
        /build/mobius-retriever

# --- Chat application source ---
# Only the subtrees the runtime actually needs. config/ and db/ go
# in because startup reads them; frontend/ is shipped because the
# FastAPI app mounts it as static files.
COPY mobius-chat/app        /build/mobius-chat/app
COPY mobius-chat/config     /build/mobius-chat/config
COPY mobius-chat/db         /build/mobius-chat/db
COPY mobius-chat/frontend   /build/mobius-chat/frontend


# ══════════════════════════════════════════════════════════════════════
# Stage 2 — runtime
# ══════════════════════════════════════════════════════════════════════
#
# python:3.13-slim with only what's required to run uvicorn + app.

FROM python:3.13-slim AS runtime

# Minimal runtime deps.
#   * ``libpq5``   — psycopg2 needs libpq at runtime (not libpq-dev, which is build-only)
#   * ``ca-certificates`` — httpx + google-cloud SDK TLS verification
# No build toolchain, no dev headers, no git.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. UID 10001 is deliberate — it stays far from
# Debian-reserved IDs (<1000) so future ``useradd`` calls during
# security patching don't collide.
RUN useradd --system --no-create-home --uid 10001 --shell /sbin/nologin chat

# Venv from the builder stage. /venv/bin is first on PATH so
# ``uvicorn`` resolves there, not to a host-level shim.
COPY --from=builder /venv /venv
ENV VIRTUAL_ENV=/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# App code. The chown happens in-COPY so the final image doesn't carry
# a separate 3× layer for a ``chown -R`` pass.
COPY --from=builder --chown=chat:chat /build/mobius-chat /app

WORKDIR /app
USER chat

# ── Runtime env defaults ─────────────────────────────────────────
#
# These are compile-time defaults; Cloud Run overrides via
# --set-env-vars / --set-secrets. Values here document what the image
# expects to find and what the sane defaults are when the env var is
# unset.

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    CHAT_QUEUE_TYPE=memory

# Cloud Run ignores EXPOSE but it's documentation for anyone running
# the image locally with ``docker run -p 8080:8080``.
EXPOSE 8080

# ── Startup ──────────────────────────────────────────────────────
#
# ``--proxy-headers`` makes Cloud Run's X-Forwarded-For / -Proto headers
# reach FastAPI middleware (the rate limiter keys on client IP; without
# this flag every request looks like it came from the proxy).
#
# ``--forwarded-allow-ips=*`` is safe because Cloud Run's front-end is
# the only thing that can reach the container port.
#
# Single worker by design: the in-process queue + background worker
# thread path requires one Python process. Horizontal scaling happens
# via Cloud Run instances, not via uvicorn workers.
CMD exec uvicorn app.main:app \
        --host 0.0.0.0 \
        --port ${PORT} \
        --proxy-headers \
        --forwarded-allow-ips='*' \
        --log-level info
