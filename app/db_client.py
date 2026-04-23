"""Lightweight client for mobius-db-agent MCP server.

Drop this file into any service that needs database access via the MCP agent.
Falls back to direct psycopg2 when the agent is unavailable.

Usage:
    from db_client import db_query, db_execute, db_get_schema

    # Read
    result = db_query("SELECT * FROM mobius_task WHERE status = :s", "chat", params={"s": "open"})
    for row in result["rows"]:
        print(row)

    # Write
    result = db_execute(
        "INSERT INTO mobius_task (task_id, type, status) VALUES (:id, :t, :s)",
        "chat",
        params={"id": "abc", "t": "review", "s": "open"},
    )
    print(result["rows_affected"])

    # Schema
    tables = db_get_schema("chat")          # list tables
    cols = db_get_schema("chat", "mobius_task")  # table columns

Environment:
    DB_AGENT_MCP_URL  - MCP server URL (default: http://localhost:8008/mcp)
    DB_AGENT_CALLER_ID - Service identity for access control (REQUIRED)
    CHAT_RAG_DATABASE_URL - Fallback direct DB URL (used when agent unavailable)
"""
import json
import logging
import os
import re
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_MCP_URL = os.environ.get("DB_AGENT_MCP_URL", "http://localhost:8008/mcp")
_CALLER_ID = os.environ.get("DB_AGENT_CALLER_ID", "")
_TIMEOUT = 15  # seconds

# 2026-04-20 monolith-beta toggle: when ``CHAT_DB_MODE=direct`` is set
# we skip the MCP agent entirely and go straight to Cloud SQL via
# psycopg2. Rationale: the Cloud Run beta runs chat as a monolith
# without db-agent deployed. Without this flag the public API would
# raise on ``_get_caller_id()`` before ever attempting the graceful
# fallback, or waste 15s per call hitting an unreachable localhost
# before the URLError catch fires.
#
# Unset (default) → normal MCP-first behavior (dev + future split).
# ``=direct``     → every call uses the psycopg2 fallback path only.
_DIRECT_MODE = (os.environ.get("CHAT_DB_MODE") or "").strip().lower() == "direct"


def _call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool via streamable-http and return the parsed result."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    req = urllib.request.Request(
        _MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # FastMCP's streamable-http transport requires text/event-stream in
            # Accept even for json_response=True. Without it the server hangs
            # negotiating the stream instead of responding with JSON.
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )

    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    body = json.loads(resp.read())

    # MCP returns result in body["result"]["content"][0]["text"]
    if "result" in body:
        content = body["result"].get("content", [])
        if content and "text" in content[0]:
            return json.loads(content[0]["text"])
    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error']}")
    return body


def _get_caller_id() -> str:
    if not _CALLER_ID:
        raise ValueError(
            "DB_AGENT_CALLER_ID env var is required. "
            "Set it to your service name (must match a manifest in mobius-db-agent/manifests/)."
        )
    return _CALLER_ID


# ---------------------------------------------------------------------------
# Direct fallback (when MCP agent is unavailable)
# ---------------------------------------------------------------------------

def _to_psycopg2_sql(sql: str) -> str:
    """Convert SQLAlchemy ``:param`` style to psycopg2 ``%(param)s`` style.

    The negative lookbehind ``(?<!:)`` is critical — PostgreSQL's type
    cast syntax ``::bigint`` / ``::text`` / ``::jsonb`` would otherwise
    have its second colon mangled into a phantom ``%(bigint)s`` /
    ``%(text)s`` parameter. The original regex didn't have this guard
    and produced user-visible errors on any query that used ``::`` casts
    (e.g. the LLM router report's ``COUNT(*)::bigint``, which surfaced
    as a literal red "'bigint'" in the UI modal).
    """
    return re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", r"%(\1)s", sql)


def _get_fallback_url(db_name: str) -> str:
    """Resolve a direct database URL from env vars.

    If ``CHAT_DB_PASSWORD`` is set (Cloud Run path — injected from
    Secret Manager), inject it into the URL so psycopg2 can auth.
    The URL otherwise carries no password (keeps ``.env.example``
    and Cloud Run env-var listings secret-free). libpq also honors
    a ``PGPASSWORD`` env var, but doing it URL-side keeps the
    semantics visible at the connect callsite.
    """
    url_map = {
        "chat": os.environ.get("CHAT_RAG_DATABASE_URL", ""),
        "rag": os.environ.get("DATABASE_URL", ""),
        "user": os.environ.get("USER_DATABASE_URL", ""),
        "qa": os.environ.get("QA_DATABASE_URL", ""),
    }
    url = url_map.get(db_name, "")
    # Strip async driver (``postgresql+psycopg2://`` → ``postgresql://``).
    url = re.sub(r"postgresql\+\w+://", "postgresql://", url)
    # Inject password if env-var-side secret is set and URL has none.
    # Matches user@ (no pw) and user:@ (empty pw) forms. Skip if URL
    # already has a password component.
    pw = os.environ.get("CHAT_DB_PASSWORD", "").strip()
    if pw and db_name == "chat" and url:
        # Only inject when the user segment lacks a ``:password``.
        m = re.match(r"(postgresql://)([^:@/]+)(@.+)$", url)
        if m:
            from urllib.parse import quote
            url = f"{m.group(1)}{m.group(2)}:{quote(pw, safe='')}{m.group(3)}"
    return url


def _fallback_error(exc: BaseException) -> dict:
    """Map a psycopg2 exception to the structured error shape used by the agent.

    Keeps the error model consistent whether the caller is hitting the MCP
    server or falling back to direct DB. Callers switch on error["code"].
    """
    sqlstate = getattr(exc, "pgcode", None)
    diag = getattr(exc, "diag", None)
    table = getattr(diag, "table_name", None) if diag else None
    column = getattr(diag, "column_name", None) if diag else None

    # Minimal SQLSTATE map duplicated from app/errors.py so db_client.py has
    # no dependency on the server-side package. Keep in sync.
    sqlstate_map = {
        "42601": "syntax_error", "42P01": "relation_missing", "42703": "column_missing",
        "23000": "integrity_violation", "23001": "integrity_violation",
        "23502": "integrity_violation", "23503": "integrity_violation",
        "23505": "integrity_violation", "23514": "integrity_violation",
        "40001": "integrity_violation", "40P01": "integrity_violation",
        "57014": "timeout", "57P01": "connection_error", "57P02": "connection_error",
        "57P03": "connection_error",
        "08000": "connection_error", "08001": "connection_error",
        "08003": "connection_error", "08004": "connection_error", "08006": "connection_error",
    }
    code = sqlstate_map.get(sqlstate or "")
    if code is None:
        msg_lower = str(exc).lower()
        if "does not exist" in msg_lower and "relation" in msg_lower:
            code = "relation_missing"
        elif "does not exist" in msg_lower and "column" in msg_lower:
            code = "column_missing"
        elif "could not connect" in msg_lower or "connection refused" in msg_lower:
            code = "connection_error"
        elif "syntax error" in msg_lower:
            code = "syntax_error"
        else:
            code = "internal"

    err: dict = {"code": code, "message": str(exc)}
    if sqlstate:
        err["sqlstate"] = sqlstate
    if table:
        err["table"] = table
    if column:
        err["column"] = column
    return {"error": err, "_fallback": True}


# ── Connection pool (2026-04-22 latency hardening) ───────────────────
#
# Every ``psycopg2.connect(url)`` over the Cloud SQL Unix socket costs
# 30–100ms just for connection setup. Chat turns do 5–10 DB ops
# (chat_turns write, chat_state read+write, progress events, feedback,
# llm_calls), so pre-pool that'd be 150ms–1s of pure overhead per turn.
# With a threaded pool, steady-state reuse drops connection cost to
# effectively zero.
#
# One pool per fallback URL (typically one in prod). Pool sizes:
#   - min=1 (always have a hot connection ready)
#   - max=CHAT_DB_POOL_MAX (default 10, matches container_concurrency=10)
# If ``psycopg2.pool`` isn't importable or pool creation fails, we
# silently fall through to the legacy per-call connect path — no
# behavior change, just lose the speedup. Loud-fail would turn a
# latency fix into an availability risk.

import threading as _threading_for_pool

_POOLS: dict[str, object] = {}
_POOLS_LOCK = _threading_for_pool.Lock()


def _get_pool_max() -> int:
    try:
        n = int((os.environ.get("CHAT_DB_POOL_MAX") or "10").strip())
        return max(1, min(50, n))  # clamp; 50 would exhaust Cloud SQL
    except (TypeError, ValueError):
        return 10


def _get_pool(url: str):
    """Return a threaded pool for ``url``, creating on first call.

    Returns None if psycopg2.pool is unavailable or pool creation fails
    — callers fall back to the legacy per-call connect path.
    """
    pool = _POOLS.get(url)
    if pool is not None:
        return pool
    with _POOLS_LOCK:
        pool = _POOLS.get(url)
        if pool is not None:
            return pool
        try:
            from psycopg2 import pool as _psycopg2_pool  # lazy import
        except ImportError:
            return None
        try:
            pool = _psycopg2_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=_get_pool_max(),
                dsn=url,
                connect_timeout=10,
            )
        except Exception as exc:
            logger.warning(
                "DB pool creation failed (%s); falling back to per-call connect",
                exc,
            )
            return None
        _POOLS[url] = pool
        logger.info("DB pool created (maxconn=%d)", _get_pool_max())
        return pool


def _acquire_conn(url: str):
    """Get a connection from the pool; fall back to a direct connect.

    Returns ``(conn, is_pooled)`` tuple. Callers MUST release via
    ``_release_conn(url, conn, is_pooled, is_broken)``.
    """
    import psycopg2

    pool = _get_pool(url)
    if pool is not None:
        try:
            conn = pool.getconn()
            # pg server may have dropped an idle conn in the pool; a
            # quick SELECT 1 confirms liveness before we use it. Cheap
            # when conn is healthy, avoids a mysterious query failure
            # when it's not.
            try:
                with conn.cursor() as _cur:
                    _cur.execute("SELECT 1")
                conn.commit()
                return conn, True
            except Exception:
                # Dead conn — return broken to the pool so it reopens,
                # then fall through to direct connect below.
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("DB pool getconn failed: %s; using direct connect", exc)
    # Legacy path
    return psycopg2.connect(url, connect_timeout=10), False


def _release_conn(url: str, conn, is_pooled: bool, is_broken: bool = False) -> None:
    if conn is None:
        return
    if is_pooled:
        pool = _POOLS.get(url)
        if pool is not None:
            try:
                pool.putconn(conn, close=is_broken)
                return
            except Exception as exc:
                logger.debug("DB pool putconn failed: %s; closing directly", exc)
    try:
        conn.close()
    except Exception:
        pass


def _fallback_query(sql: str, db_name: str, params: dict, max_rows: int) -> dict:
    """Direct psycopg2 query when MCP agent is down."""
    import psycopg2  # noqa: F401 — keep import for side-effect compatibility
    import psycopg2.extras  # noqa: F401

    url = _get_fallback_url(db_name)
    if not url:
        return {
            "error": {"code": "connection_error",
                      "message": f"No fallback URL for database '{db_name}'"},
            "_fallback": True,
        }

    try:
        conn, is_pooled = _acquire_conn(url)
    except Exception as exc:
        return _fallback_error(exc)

    broken = False
    try:
        with conn.cursor() as cur:
            cur.execute(_to_psycopg2_sql(sql), params or None)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
            return {
                "columns": columns,
                "rows": [list(r) for r in rows],
                "row_count": len(rows),
                "truncated": len(rows) == max_rows,
                "_fallback": True,
            }
    except Exception as exc:
        broken = True
        return _fallback_error(exc)
    finally:
        _release_conn(url, conn, is_pooled, is_broken=broken)


def _fallback_execute(sql: str, db_name: str, params: dict) -> dict:
    """Direct psycopg2 execute when MCP agent is down."""
    url = _get_fallback_url(db_name)
    if not url:
        return {
            "error": {"code": "connection_error",
                      "message": f"No fallback URL for database '{db_name}'"},
            "_fallback": True,
        }

    try:
        conn, is_pooled = _acquire_conn(url)
    except Exception as exc:
        return _fallback_error(exc)

    broken = False
    try:
        with conn.cursor() as cur:
            cur.execute(_to_psycopg2_sql(sql), params or None)
            rows_affected = cur.rowcount
        conn.commit()
        return {"rows_affected": rows_affected, "_fallback": True}
    except Exception as exc:
        broken = True
        try:
            conn.rollback()
        except Exception:
            pass
        return _fallback_error(exc)
    finally:
        _release_conn(url, conn, is_pooled, is_broken=broken)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def db_query(
    sql: str,
    db_name: str,
    params: dict | None = None,
    max_rows: int = 1000,
) -> dict:
    """Execute a read-only SQL query. Falls back to direct DB if agent is down."""
    if _DIRECT_MODE:
        return _fallback_query(sql, db_name, params or {}, max_rows)
    try:
        return _call_mcp_tool("db_query", {
            "sql": sql,
            "db_name": db_name,
            "caller_id": _get_caller_id(),
            "params": json.dumps(params or {}),
            "max_rows": max_rows,
        })
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        logger.warning("db-agent unavailable (%s), falling back to direct DB", exc)
        return _fallback_query(sql, db_name, params or {}, max_rows)


def db_execute(
    sql: str,
    db_name: str,
    params: dict | None = None,
) -> dict:
    """Execute a write SQL statement. Falls back to direct DB if agent is down."""
    if _DIRECT_MODE:
        return _fallback_execute(sql, db_name, params or {})
    try:
        return _call_mcp_tool("db_execute", {
            "sql": sql,
            "db_name": db_name,
            "caller_id": _get_caller_id(),
            "params": json.dumps(params or {}),
        })
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        logger.warning("db-agent unavailable (%s), falling back to direct DB", exc)
        return _fallback_execute(sql, db_name, params or {})


def db_get_schema(
    db_name: str,
    table: str = "",
) -> dict:
    """Get schema info. No fallback — requires the agent."""
    return _call_mcp_tool("db_get_schema", {
        "db_name": db_name,
        "caller_id": _get_caller_id(),
        "table": table,
    })


def db_transaction(
    statements: list[dict],
    db_name: str,
) -> dict:
    """Execute multiple INSERT/UPDATE/DELETE statements as one atomic transaction.

    Args:
        statements: list of ``{"sql": "<:param style SQL>", "params": {...}}``
                    — all statements must be writes (no SELECT, no DDL).
        db_name: target database name (chat / rag / user / qa).

    Success returns:
        {
            "statements_executed": N,
            "rows_affected_total": M,
            "per_statement": [{"operation": "INSERT", "table": "t",
                                "rows_affected": K}, ...],
        }

    Failure returns structured ``{"error": {"code": ..., "message": ...,
    "statement_index": N}}`` — any failure rolls back the entire transaction,
    so callers never see partial writes.

    No direct-DB fallback historically; 2026-04-20 added a ``CHAT_DB_MODE=direct``
    path for the Cloud Run monolith beta where db-agent isn't deployed.
    That path runs all statements inside a single psycopg2 transaction
    with the same rollback-on-failure semantics.
    """
    if _DIRECT_MODE:
        return _fallback_transaction(statements, db_name)
    return _call_mcp_tool("db_transaction", {
        "statements": json.dumps(statements),
        "db_name": db_name,
        "caller_id": _get_caller_id(),
    })


def _fallback_transaction(statements: list[dict], db_name: str) -> dict:
    """Direct psycopg2 implementation of ``db_transaction``.

    Runs every statement in one transaction, rolls back on any error,
    and returns the same shape as the MCP version so callers can't
    tell the difference.
    """
    url = _get_fallback_url(db_name)
    if not url:
        return {
            "error": {"code": "connection_error",
                      "message": f"No fallback URL for database '{db_name}'"},
            "_fallback": True,
        }
    try:
        conn, is_pooled = _acquire_conn(url)
    except Exception as exc:
        return _fallback_error(exc)

    per_stmt: list[dict] = []
    total = 0
    broken = False
    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements):
                sql = stmt.get("sql", "")
                params = stmt.get("params") or {}
                try:
                    cur.execute(_to_psycopg2_sql(sql), params)
                except Exception as exc:
                    conn.rollback()
                    err = _fallback_error(exc)
                    if "error" in err:
                        err["error"]["statement_index"] = i
                    return err
                # Parse operation + table for the response shape. Best-effort.
                op = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
                tbl = ""
                m = re.search(r"(?:INTO|UPDATE|FROM)\s+([A-Za-z_][\w.]*)", sql, re.I)
                if m:
                    tbl = m.group(1)
                per_stmt.append({
                    "operation": op,
                    "table": tbl,
                    "rows_affected": cur.rowcount,
                })
                total += max(0, cur.rowcount)
        conn.commit()
        return {
            "statements_executed": len(statements),
            "rows_affected_total": total,
            "per_statement": per_stmt,
            "_fallback": True,
        }
    except Exception as exc:
        broken = True
        try:
            conn.rollback()
        except Exception:
            pass
        return _fallback_error(exc)
    finally:
        _release_conn(url, conn, is_pooled, is_broken=broken)


# ─────────────────────────────────────────────────────────────────────────
# Shared error helpers
# ─────────────────────────────────────────────────────────────────────────
#
# Every storage module that calls ``db_query`` / ``db_execute`` has the
# same two helpers for reading the structured error shape. Having them
# in the client (not duplicated across 5 storage modules) means one
# source of truth for what an error looks like — and new storage modules
# stay in sync by construction.
#
# The shape contract (matches app.db_agent / mobius-db-agent / contract):
#   {"error": {"code": "...", "message": "...", ...extras}}
#
# ``_err_code`` / ``_err_message`` are tolerant of older shapes where
# ``error`` was a bare string (can happen on the direct-psycopg2 fallback
# path when the caller hasn't updated to the structured shape yet).


def err_code(result: dict) -> str | None:
    """Return ``result["error"]["code"]`` when the result is an error, else None.

    ``result`` is the return value of ``db_query`` / ``db_execute``.
    On success there's no ``error`` key; on error the shape is
    ``{"error": {"code": "...", ...}}``. This helper normalizes both
    so callers can do::

        if err_code(result) == "connection_error":
            ...
    """
    err = result.get("error") if isinstance(result, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return None


def err_message(result: dict) -> str:
    """Return the human-readable error message or "" on success.

    Handles both the canonical dict shape and the legacy bare-string
    shape (pre-2026-04-20 fallback path).
    """
    err = result.get("error") if isinstance(result, dict) else None
    if isinstance(err, dict):
        return err.get("message", "") or ""
    return str(err) if err else ""


# Keep the underscore-prefixed aliases for back-compat with storage
# modules that imported ``_err_code`` / ``_err_message`` as private
# helpers before they moved here. New code should import the unprefixed
# names.
_err_code = err_code
_err_message = err_message
