"""Run mobius-chat DB migrations at startup. Apply each SQL file in db/schema/ in order.
Uses CHAT_RAG_DATABASE_URL from .env. If unset, skips and exits 0."""
import logging
import os
import sys
from pathlib import Path

# Load .env before reading CHAT_RAG_DATABASE_URL (module .env first, then mobius-config/.env)
_chat_root = Path(__file__).resolve().parent.parent
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

    import psycopg2

    conn = psycopg2.connect(url)
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
