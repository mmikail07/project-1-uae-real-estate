"""SQLite connection + schema bootstrap.

Usage:
    python -m src.db --init           # create schema (idempotent)
    python -m src.db --reset          # drop file and recreate
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src.config import DB_PATH, SQL_DIR


@contextmanager
def connect(db_path: Path = DB_PATH):
    """Yield a SQLite connection with sane defaults (foreign keys ON, row factory dict-like)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: Path = DB_PATH) -> None:
    """Apply sql/schema.sql to the DB. Safe to run repeatedly (uses IF NOT EXISTS).
    Also runs migrate() to add columns that don't exist on older DBs."""
    schema_sql = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(schema_sql)
    print(f"[db] schema applied -> {db_path}")
    migrate(db_path)


def ensure_column(conn, table: str, column: str, decl: str) -> bool:
    """ADD COLUMN if missing. SQLite lacks IF NOT EXISTS for columns, so we
    check PRAGMA table_info first. Returns True if a column was added."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# Migrations applied to existing DBs. Schema.sql defines the target state;
# this list catches existing DBs up. Each entry is (table, column, declaration).
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("areas",        "display_name",     "TEXT"),
    ("transactions", "bedroom_category", "TEXT"),
]


def migrate(db_path: Path = DB_PATH) -> None:
    """Idempotently add columns missing on existing DBs."""
    added: list[str] = []
    with connect(db_path) as conn:
        for table, column, decl in _MIGRATIONS:
            if ensure_column(conn, table, column, decl):
                added.append(f"{table}.{column}")
        # Indexes on the new columns (only created after column exists)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_bedroom_cat ON transactions(bedroom_category)")
    if added:
        print(f"[db] migrated: added {', '.join(added)}")
    else:
        print("[db] migrate: no changes (schema already current)")


def apply_views(db_path: Path = DB_PATH) -> None:
    """Apply sql/derived_views.sql — idempotent via DROP VIEW IF EXISTS in the file."""
    views_path = SQL_DIR / "derived_views.sql"
    if not views_path.exists():
        print(f"[db] no views file at {views_path}")
        return
    sql = views_path.read_text(encoding="utf-8")
    with connect(db_path) as conn:
        conn.executescript(sql)
        view_names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name"
        )]
    print(f"[db] views applied: {', '.join(view_names) if view_names else '(none created)'}")


def reset(db_path: Path = DB_PATH) -> None:
    if db_path.exists():
        db_path.unlink()
        print(f"[db] deleted {db_path}")
    init_schema(db_path)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="SQLite schema management")
    parser.add_argument("--init",    action="store_true", help="apply schema (idempotent)")
    parser.add_argument("--migrate", action="store_true", help="add columns missing on existing DBs")
    parser.add_argument("--views",   action="store_true", help="apply derived_views.sql")
    parser.add_argument("--reset",   action="store_true", help="delete DB file and recreate")
    args = parser.parse_args()

    if args.reset:
        reset()
    elif args.init:
        init_schema()
    elif args.migrate:
        migrate()
    elif args.views:
        apply_views()
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
