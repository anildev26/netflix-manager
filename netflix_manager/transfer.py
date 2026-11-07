"""Copy a Netflix Manager database without printing customer rows or credentials.

Source connections are read-only. Target writes happen only inside a transaction
that commits when the operator passes commit=True. Restore refuses a target that
already has rows. Nothing here drops tables.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg2

from netflix_manager.schema import apply_schema

KNOWN_TABLES = (
    "customers",
    "reminder_deliveries",
    "telegram_updates",
    "telegram_workflows",
)
TABLE_KEYS = {
    "customers": "id",
    "reminder_deliveries": "id",
    "telegram_updates": "update_id",
    "telegram_workflows": "id",
}
REQUIRED_TABLES = ("customers",)
IDENTIFIER = __import__("re").compile(r"^[a-z_][a-z0-9_]*$")


class TransferError(RuntimeError):
    pass


def identity(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path:
        raise TransferError("database URL is not a postgres URL")
    port = parsed.port or 5432
    name = parsed.path.lstrip("/")
    if not name or "/" in name:
        raise TransferError("database URL is not a postgres URL")
    return f"{parsed.hostname}:{port}/{name}"


def backup(conn, *, ignore_extra_tables=False) -> dict:
    conn.set_session(readonly=True, autocommit=False)
    try:
        present = _tables(conn)
        extra = [name for name in present if name not in KNOWN_TABLES]
        if extra and not ignore_extra_tables:
            raise TransferError("source has tables this tool does not copy: " + ", ".join(extra))
        if "customers" not in present:
            raise TransferError("source has no customers table")
        snapshot = {"format": 1, "tables": {}, "ignored_extra_tables": extra}
        for table in KNOWN_TABLES:
            if table not in present:
                continue
            columns = _columns(conn, table)
            rows = _select_rows(conn, table, columns)
            snapshot["tables"][table] = {
                "columns": columns,
                "rows": [_jsonable(row, columns) for row in rows],
                "sequence": _sequence_state(conn, table),
            }
        return snapshot
    finally:
        conn.rollback()


def restore(conn, snapshot: dict, *, commit: bool) -> dict:
    _require_snapshot(snapshot)
    if _target_counts(conn, snapshot):
        raise TransferError("target already has rows; restore will not overwrite")
    # setval is not undone by rollback, so a dry-run restores the old sequence values.
    prior = {table: _sequence_state(conn, table) for table in snapshot["tables"]}
    inserted = {}
    committed = False
    try:
        for table, body in snapshot["tables"].items():
            _assert_same_columns(conn, table, body["columns"])
            _insert_rows(conn, table, body["columns"], body["rows"])
            _restore_sequence(conn, table, body.get("sequence"))
            inserted[table] = len(body["rows"])
        problems = validate(conn, snapshot)
        if problems:
            raise TransferError("restore validation failed: " + "; ".join(problems))
        if commit:
            conn.commit()
            committed = True
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        if not committed:
            _put_back_sequences(conn, prior)
    return {"inserted": inserted, "committed": committed}


def validate(conn, snapshot: dict) -> list[str]:
    problems = []
    for table, body in snapshot["tables"].items():
        if not _table_exists(conn, table):
            problems.append(f"{table} missing")
            continue
        columns = _columns(conn, table)
        if columns != body["columns"]:
            problems.append(f"{table} columns differ")
            continue
        current = [_jsonable(row, columns) for row in _select_rows(conn, table, columns)]
        expected = list(body["rows"])
        current_hashes = sorted(_row_hash(row, columns) for row in current)
        expected_hashes = sorted(_row_hash(row, columns) for row in expected)
        if current_hashes != expected_hashes:
            problems.append(f"{table} rows differ")
        seq = _sequence_state(conn, table)
        wanted = body.get("sequence")
        if (seq or {}).get("last_value") != (wanted or {}).get("last_value") or (seq or {}).get("is_called") != (wanted or {}).get("is_called"):
            if wanted or seq:
                problems.append(f"{table} sequence differs")
    return problems


def diff(conn, snapshot: dict) -> dict:
    """Count inserts, updates, and deletes versus a snapshot. Ids only, no cell values."""
    conn.set_session(readonly=True, autocommit=False)
    report = {}
    for table, body in snapshot["tables"].items():
        columns = body["columns"]
        _assert_same_columns(conn, table, columns)
        key = TABLE_KEYS[table]
        current = {row[key]: _jsonable(row, columns) for row in _select_rows(conn, table, columns)}
        original = {row[key]: row for row in body["rows"]}
        inserts, updates, deletes = [], [], []
        for cid, row in current.items():
            if cid not in original:
                inserts.append(cid)
            elif _row_hash(row, columns) != _row_hash(original[cid], columns):
                updates.append(cid)
        for cid in original:
            if cid not in current:
                deletes.append(cid)
        report[table] = {
            "inserts": sorted(inserts),
            "updates": sorted(updates),
            "deletes": sorted(deletes),
        }
    return report


def apply_delta(from_conn, onto_conn, snapshot: dict, *, commit: bool, propagate_deletes: bool) -> dict:
    """Copy rows that differ from the snapshot out of from_conn onto onto_conn.

    Used to move post-cutover writes back before pointing the app at the old
    database. Deletes are reported and applied only when propagate_deletes is set.
    """
    changes = diff(from_conn, snapshot)
    prior = {table: _sequence_state(onto_conn, table) for table in snapshot["tables"]}
    applied = {}
    committed = False
    try:
        for table, body in snapshot["tables"].items():
            columns = body["columns"]
            _assert_same_columns(onto_conn, table, columns)
            key = TABLE_KEYS[table]
            source_rows = {row[key]: row for row in _select_rows(from_conn, table, columns)}
            change = changes[table]
            for cid in change["inserts"] + change["updates"]:
                row = _jsonable(source_rows[cid], columns)
                if cid in change["updates"]:
                    assignments = ", ".join(f"{column}=%s" for column in columns if column != key)
                    params = tuple(row[column] for column in columns if column != key) + (cid,)
                    _execute(onto_conn, f"UPDATE {table} SET {assignments} WHERE {key}=%s", params)
                else:
                    _insert_rows(onto_conn, table, columns, [row])
            deleted = 0
            if propagate_deletes:
                for cid in change["deletes"]:
                    _execute(onto_conn, f"DELETE FROM {table} WHERE {key}=%s", (cid,))
                    deleted += 1
            elif change["deletes"]:
                pass
            _restore_sequence(onto_conn, table, _sequence_state(from_conn, table))
            applied[table] = {
                "inserts": len(change["inserts"]),
                "updates": len(change["updates"]),
                "deletes": deleted,
                "deletes_not_applied": 0 if propagate_deletes else len(change["deletes"]),
            }
        if commit:
            onto_conn.commit()
            committed = True
        else:
            onto_conn.rollback()
    except Exception:
        onto_conn.rollback()
        raise
    finally:
        if not committed:
            _put_back_sequences(onto_conn, prior)
    return {"applied": applied, "committed": committed}


def connect(url: str):
    try:
        return psycopg2.connect(url)
    except Exception:
        raise TransferError("database connection failed for " + identity(url)) from None


def apply_migrations(conn) -> list[str]:
    applied = apply_schema(conn)
    conn.commit()
    return applied


def _tables(conn) -> list[str]:
    rows = _execute(conn, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1", fetch="all")
    return [row[0] for row in rows]


def _table_exists(conn, table: str) -> bool:
    return table in _tables(conn)


def _columns(conn, table: str) -> list[str]:
    rows = _execute(
        conn,
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='public' AND table_name=%s
           ORDER BY ordinal_position""",
        (table,),
        fetch="all",
    )
    columns = [row[0] for row in rows]
    for column in columns:
        if not IDENTIFIER.fullmatch(column):
            raise TransferError("unexpected column name")
    if TABLE_KEYS[table] not in columns:
        raise TransferError(f"{table} has no {TABLE_KEYS[table]} column")
    return columns


def _select_rows(conn, table, columns):
    if table not in KNOWN_TABLES:
        raise TransferError("refusing table")
    sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {TABLE_KEYS[table]}"
    return _execute(conn, sql, fetch="dicts", columns=columns)


def _insert_rows(conn, table, columns, rows):
    if table not in KNOWN_TABLES:
        raise TransferError("refusing table")
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    cur = conn.cursor()
    try:
        for row in rows:
            cur.execute(sql, tuple(row[column] for column in columns))
    finally:
        cur.close()


def _sequence_state(conn, table):
    if table not in KNOWN_TABLES:
        raise TransferError("refusing table")
    row = _execute(conn, "SELECT pg_get_serial_sequence(%s, %s)", (table, TABLE_KEYS[table]), fetch="one")
    seq = row[0] if row else None
    if not seq:
        return None
    # seq is a regclass returned as text like public.customers_id_seq
    if not all(part.replace("_", "").isalnum() for part in seq.split(".")):
        raise TransferError("unexpected sequence name")
    quoted = ".".join(f'"{part}"' for part in seq.split("."))
    state = _execute(conn, f"SELECT last_value, is_called FROM {quoted}", fetch="one")
    return {"last_value": int(state[0]), "is_called": bool(state[1])}


def _put_back_sequences(conn, prior):
    try:
        for table, state in prior.items():
            _restore_sequence(conn, table, state)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _restore_sequence(conn, table, state):
    if not state:
        return
    row = _execute(conn, "SELECT pg_get_serial_sequence(%s, %s)", (table, TABLE_KEYS[table]), fetch="one")
    seq = row[0] if row else None
    if not seq:
        return
    _execute(conn, "SELECT setval(%s, %s, %s)", (seq, int(state["last_value"]), bool(state["is_called"])), fetch="one")


def _assert_same_columns(conn, table, columns):
    if table not in KNOWN_TABLES or not _table_exists(conn, table):
        raise TransferError(f"{table} is missing on the destination")
    current = _columns(conn, table)
    if current != list(columns):
        raise TransferError(f"{table} column mismatch: " + ", ".join(sorted(set(current) ^ set(columns))))


def _target_counts(conn, snapshot) -> int:
    total = 0
    for table in snapshot["tables"]:
        if not _table_exists(conn, table):
            raise TransferError(f"{table} is missing on the destination")
        row = _execute(conn, f"SELECT COUNT(*) FROM {table}", fetch="one")
        total += int(row[0])
    return total


def _require_snapshot(snapshot: dict):
    if snapshot.get("format") != 1 or "customers" not in snapshot.get("tables", {}):
        raise TransferError("snapshot is not a netflix manager backup")
    for table in snapshot["tables"]:
        if table not in KNOWN_TABLES:
            raise TransferError("snapshot contains an unknown table")


def _execute(conn, sql, params=None, fetch="none", columns=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if fetch == "all":
            return cur.fetchall()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "dicts":
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        return None
    finally:
        cur.close()


def _jsonable(row: dict, columns) -> dict:
    return {column: _norm(row[column]) for column in columns}


def _norm(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 6)
    return value


def _row_hash(row: dict, columns) -> str:
    payload = json.dumps([row[column] for column in columns], separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
