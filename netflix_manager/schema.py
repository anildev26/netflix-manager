"""SQL files applied only by the operator tool or an explicit setup call.

Website and cron requests do not execute these files.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DIR = ROOT / "migrations"
MIGRATION_FILES = (
    "001_customers.sql",
    "002_reminder_deliveries.sql",
    "003_telegram.sql",
)
CUSTOMERS_FILE = "001_customers.sql"
FORBIDDEN_SQL = re.compile(
    r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER|GRANT|REVOKE|COPY|INSERT)\b",
    re.IGNORECASE,
)


class UnsafeMigration(RuntimeError):
    pass


def _sql_without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--.*?$", " ", text, flags=re.M)


def read_migration(name: str) -> str:
    if name not in MIGRATION_FILES:
        raise UnsafeMigration("unknown migration")
    text = (MIGRATION_DIR / name).read_text(encoding="utf-8")
    if FORBIDDEN_SQL.search(_sql_without_comments(text)):
        raise UnsafeMigration("migration file contains a forbidden statement")
    return text


def customers_ddl() -> str:
    return read_migration(CUSTOMERS_FILE)


def apply_schema(conn) -> list[str]:
    """Run the idempotent CREATE statements. Caller commits."""
    applied = []
    cur = conn.cursor()
    try:
        for name in MIGRATION_FILES:
            cur.execute(read_migration(name))
            applied.append(name)
    finally:
        cur.close()
    return applied
