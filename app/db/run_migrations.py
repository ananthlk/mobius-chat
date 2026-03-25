"""Run mobius-chat DB migrations at startup. Apply each SQL file in db/schema/ in order.
Uses CHAT_RAG_DATABASE_URL from .env. If unset, skips and exits 0."""
import logging
import os
import sys
from pathlib import Path

# Load .env before reading CHAT_RAG_DATABASE_URL (module .env first, then mobius-config/.env)
# This file lives at app/db/run_migrations.py — repo root is three levels up (same as seed.py / db __main__).
_chat_root = Path(__file__).resolve().parent.parent.parent
_config_dir = _chat_root.parent / "mobius-config"
if _config_dir.exists() and str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))
try:
    from env_helper import load_env
    load_env(_chat_root)
except ImportError:
    from dotenv import load_dotenv
    # 1) mobius-chat/.env
    env_file = _chat_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)
    # 2) mobius-config/.env (fill in only what is not set)
    global_env = _config_dir / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    # Keep in sync with app.chat_config._build_rag_from_env fallbacks.
    return (
        os.environ.get("CHAT_RAG_DATABASE_URL")
        or os.environ.get("RAG_DATABASE_URL")
        or os.environ.get("CHAT_DATABASE_URL")  # used by mobius-dbt as chat destination URL
        or ""
    ).strip()


def _is_connection_slot_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "connection slots" in msg or "too many clients" in msg


def _slot_exhaustion_hint() -> str:
    return (
        "No free PostgreSQL connection slots (the server is not accepting new connections). "
        "Stop other clients (other machines, CI, dashboards), then run `mstart --restart-db` "
        "to restart the Cloud SQL instance, or increase max_connections for this instance in GCP."
    )


def _merge_admin_for_migrate(admin_url: str, app_url: str) -> str:
    """Use superuser credentials/host from admin_url with database name from app_url (e.g. mobius_chat)."""
    import urllib.parse

    au = admin_url.strip().replace("postgresql+asyncpg://", "postgresql://")
    pu = app_url.strip().replace("postgresql+asyncpg://", "postgresql://")
    try:
        from sqlalchemy.engine import make_url

        a = make_url(au)
        p = make_url(pu)
        db = (p.database or "mobius_chat").lstrip("/")
        # str(URL) masks password as "***" — never parse that; psycopg2 would send wrong password.
        merged = a.set(database=db)
        rs = getattr(merged, "render_as_string", None)
        if callable(rs):
            return rs(hide_password=False)
        return str(merged)
    except Exception:
        ap = urllib.parse.urlparse(au)
        pp = urllib.parse.urlparse(pu)
        app_db = (pp.path or "/mobius_chat").strip("/") or "mobius_chat"
        return urllib.parse.urlunparse((ap.scheme, ap.netloc, "/" + app_db, "", "", ""))


def _connect_db(url: str):
    """Connect using URL; parse into components to handle special chars in password."""
    import urllib.parse

    try:
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        return __import__("psycopg2").connect(
            host=parsed.host or "localhost",
            port=parsed.port or 5432,
            dbname=(parsed.database or "postgres").lstrip("/"),
            user=parsed.username or "postgres",
            password=parsed.password or "",
            connect_timeout=10,
        )
    except ImportError:
        pass

    # Fallback: parse postgresql://user:password@host:port/db manually
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc
    path = (parsed.path or "/").lstrip("/") or "postgres"
    userinfo, _, hostport = netloc.rpartition("@")
    if not hostport:
        return __import__("psycopg2").connect(url)
    username, _, password = userinfo.partition(":")
    password = urllib.parse.unquote_to_bytes(password).decode("utf-8", "replace")
    host, _, port_str = hostport.rpartition(":")
    port = int(port_str) if port_str.isdigit() else 5432
    return __import__("psycopg2").connect(
        host=host or "localhost",
        port=port,
        dbname=path,
        user=urllib.parse.unquote(username) if username else "postgres",
        password=password,
        connect_timeout=10,
    )


def _connect_for_migrations(app_url: str):
    """Connect for migrations: prefer superuser merge when CHAT_RAG_DATABASE_ADMIN_URL is set (slot exhaustion)."""
    import psycopg2

    admin = (os.environ.get("CHAT_RAG_DATABASE_ADMIN_URL") or "").strip()
    if admin:
        try:
            merged = _merge_admin_for_migrate(admin, app_url)
            return _connect_db(merged)
        except psycopg2.OperationalError as e:
            if _is_connection_slot_error(e):
                logger.error("%s %s", e, _slot_exhaustion_hint())
                raise
            logger.warning(
                "CHAT_RAG_DATABASE_ADMIN_URL connect failed (%s); falling back to app URL",
                e,
            )
        except Exception as e:
            if _is_connection_slot_error(e):
                logger.error("%s %s", e, _slot_exhaustion_hint())
                raise
            logger.warning(
                "CHAT_RAG_DATABASE_ADMIN_URL connect failed (%s); falling back to app URL",
                e,
            )

    try:
        return _connect_db(app_url)
    except psycopg2.OperationalError as e:
        if not _is_connection_slot_error(e):
            raise
        if not admin:
            logger.error("%s %s", e, _slot_exhaustion_hint())
            raise
        merged = _merge_admin_for_migrate(admin, app_url)
        logger.warning(
            "Primary DB connect failed (connection limit); retrying migrations with CHAT_RAG_DATABASE_ADMIN_URL"
        )
        try:
            return _connect_db(merged)
        except psycopg2.OperationalError as e2:
            if _is_connection_slot_error(e2):
                logger.error("%s %s", e2, _slot_exhaustion_hint())
            raise


def run_migrations() -> bool:
    """Run each .sql in db/schema/ in sorted order. Returns True if ran, False if skipped (no URL)."""
    url = _get_db_url()
    if not url:
        logger.info("CHAT_RAG_DATABASE_URL not set; skipping migrations")
        return False

    schema_dir = _chat_root / "db" / "schema"
    if not schema_dir.is_dir():
        # Fallback when run from mobius-chat cwd (e.g. mstart: cd mobius-chat && python -m app.db.run_migrations)
        schema_dir = Path(os.getcwd()) / "db" / "schema"
    if not schema_dir.is_dir():
        logger.info("No db/schema dir; skipping migrations")
        return False

    files = sorted(schema_dir.glob("*.sql"))
    if not files:
        logger.info("No .sql files in db/schema; skipping migrations")
        return False

    conn = _connect_for_migrations(url)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        for path in files:
            sql = path.read_text()
            for stmt in _split_statements(sql):
                if stmt:
                    cur.execute(stmt)
            logger.info("Applied %s", path.name)
    finally:
        cur.close()
        conn.close()
    return True


def _split_statements(sql: str):
    """Split SQL into statements by semicolon followed by newline. Keeps semicolon in statement."""
    out = []
    current = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                out.append(stmt)
            current = []
    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            out.append(stmt)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[migrations] %(message)s")
    try:
        run_migrations()
    except Exception as e:
        logger.exception("Migrations failed: %s", e)
        sys.exit(1)
