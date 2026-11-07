import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest

from netflix_manager.repos import PgRepos, STALE_MINUTES
from netflix_manager.schema import FORBIDDEN_SQL, MIGRATION_FILES, _sql_without_comments, read_migration
from netflix_manager.transfer import (
    TransferError,
    apply_delta,
    apply_migrations,
    backup,
    connect,
    diff,
    identity,
    restore,
)

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "postgresql://nmtest:nmtest-local-only@127.0.0.1:5432/postgres"


def _local(url):
    host = urlsplit(url).hostname
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("refusing non-local database")


@pytest.fixture
def databases():
    _local(ADMIN)
    names = [f"nmtest_{uuid.uuid4().hex[:10]}" for _ in range(3)]
    admin = psycopg2.connect(ADMIN)
    admin.autocommit = True
    cur = admin.cursor()
    for name in names:
        cur.execute(f'CREATE DATABASE "{name}"')
    cur.close()
    admin.close()
    urls = [f"postgresql://nmtest:nmtest-local-only@127.0.0.1:5432/{name}" for name in names]
    try:
        yield urls
    finally:
        admin = psycopg2.connect(ADMIN)
        admin.autocommit = True
        cur = admin.cursor()
        for name in names:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.close()
        admin.close()


def _seed(url):
    conn = connect(url)
    apply_migrations(conn)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO customers
           (name, phone, account, profile_name, monthly_amount, start_date, payment_status, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        ("Fixture Ada", "910000000001", "Fixture Account", "Fixture Profile", 10, "2026-01-15", "Paid", "2026-01-01"),
    )
    cur.execute(
        """INSERT INTO customers
           (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        ("Fixture Bo", "910000000002", "Fixture Account", "Fixture Profile", 12, "2026-02-01", "Payment pending"),
    )
    cur.execute("DELETE FROM customers WHERE name = %s", ("Fixture Bo",))
    conn.commit()
    cur.close()
    conn.close()


def test_migrations_are_non_destructive():
    for name in MIGRATION_FILES:
        text = read_migration(name)
        assert FORBIDDEN_SQL.search(_sql_without_comments(text)) is None
        assert "DROP" not in text.upper() or "--" in text


def test_round_trip_ids_created_at_and_sequence(databases, tmp_path):
    source, target, onto = databases
    _seed(source)
    src = connect(source)
    snapshot = backup(src)
    src.close()
    assert snapshot["tables"]["customers"]["rows"][0]["created_at"] == "2026-01-01"
    assert snapshot["tables"]["customers"]["sequence"]["last_value"] == 2
    assert snapshot["tables"]["customers"]["sequence"]["is_called"] is True
    dst = connect(target)
    apply_migrations(dst)
    dry = restore(dst, snapshot, commit=False)
    assert dry["committed"] is False
    count = dst.cursor()
    count.execute("SELECT COUNT(*) FROM customers")
    assert count.fetchone()[0] == 0
    count.close()
    dst.close()
    fresh = connect(target)
    cur = fresh.cursor()
    cur.execute(
        """INSERT INTO customers (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES ('Temp', '910000000009', 'A', 'P', 1, '2026-03-01', 'Paid') RETURNING id"""
    )
    assert cur.fetchone()[0] == 1
    fresh.rollback()
    fresh.close()
    dst = connect(target)
    done = restore(dst, snapshot, commit=True)
    dst.close()
    assert done["committed"] is True
    check = connect(target)
    problems = []
    check.set_session(readonly=True, autocommit=False)
    from netflix_manager.transfer import validate
    problems = validate(check, snapshot)
    cur = check.cursor()
    cur.execute("SELECT id, created_at FROM customers ORDER BY id")
    rows = cur.fetchall()
    cur.execute("SELECT last_value, is_called FROM customers_id_seq")
    seq = cur.fetchone()
    check.close()
    assert problems == []
    assert rows == [(1, "2026-01-01")]
    assert seq[0] == 2 and seq[1] is True
    writer = connect(target)
    cur = writer.cursor()
    cur.execute(
        """INSERT INTO customers (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES ('Next', '910000000003', 'A', 'P', 1, '2026-03-01', 'Paid') RETURNING id"""
    )
    assert cur.fetchone()[0] == 3
    writer.rollback()
    writer.close()

    live = connect(target)
    cur = live.cursor()
    cur.execute("UPDATE customers SET monthly_amount = 99 WHERE id = 1")
    cur.execute(
        """INSERT INTO customers (name, phone, account, profile_name, monthly_amount, start_date, payment_status, created_at)
           VALUES ('Fixture New', '910000000004', 'Fixture Account', 'Fixture Profile', 15, '2026-03-02', 'Paid', '2026-03-02')"""
    )
    live.commit()
    report = diff(live, snapshot)
    live.close()
    assert report["customers"]["updates"] == [1]
    assert report["customers"]["inserts"]
    blob = json.dumps(report)
    assert "910000000001" not in blob
    assert "Fixture Ada" not in blob

    original = connect(onto)
    apply_migrations(original)
    restore(original, snapshot, commit=True)
    changed = connect(target)
    preview = apply_delta(changed, original, snapshot, commit=False, propagate_deletes=False)
    assert preview["committed"] is False
    cur = original.cursor()
    cur.execute("SELECT COUNT(*) FROM customers")
    assert cur.fetchone()[0] == 1
    cur.close()
    original.close()
    changed.close()

    original = connect(onto)
    changed = connect(target)
    apply_delta(changed, original, snapshot, commit=True, propagate_deletes=False)
    original.close()
    changed.close()
    check = connect(onto)
    cur = check.cursor()
    cur.execute("SELECT id, monthly_amount, created_at FROM customers ORDER BY id")
    found = cur.fetchall()
    check.close()
    assert (1, 99, "2026-01-01") in [(row[0], row[1] if not isinstance(row[1], float) else int(row[1]) if float(row[1]).is_integer() else row[1], row[2]) for row in found]
    assert any(row[2] == "2026-03-02" for row in found)


def test_cli_redacts_secrets_and_refuses_inline_urls(databases, tmp_path):
    source, target, _onto = databases
    _seed(source)
    connect(target).close()
    # target has no schema yet; dry-run should fail closed without printing the phone or password.
    env = os.environ.copy()
    env["SOURCE_DATABASE_URL"] = source
    env["TARGET_DATABASE_URL"] = target
    refused = subprocess.run(
        [sys.executable, "scripts/transfer_db.py", "postgresql://user:super-secret@127.0.0.1:5432/db"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert refused.returncode == 2
    assert "super-secret" not in refused.stdout
    assert "super-secret" not in refused.stderr
    mismatch = subprocess.run(
        [sys.executable, "scripts/transfer_db.py", "dry-run",
         "--confirm-source", "wrong:1/nope", "--confirm-target", identity(target)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert mismatch.returncode == 1
    assert "nmtest-local-only" not in mismatch.stdout + mismatch.stderr
    assert "910000000001" not in mismatch.stdout + mismatch.stderr
    prepared = connect(target)
    apply_migrations(prepared)
    prepared.close()
    run = subprocess.run(
        [sys.executable, "scripts/transfer_db.py", "dry-run",
         "--confirm-source", identity(source), "--confirm-target", identity(target)],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    assert run.returncode == 0, run.stderr
    assert "nmtest-local-only" not in run.stdout + run.stderr
    assert "910000000001" not in run.stdout
    assert "Fixture Ada" not in run.stdout
    assert "committed false" in run.stdout
    same = subprocess.run(
        [sys.executable, "scripts/transfer_db.py", "dry-run",
         "--confirm-source", identity(source), "--confirm-target", identity(source)],
        cwd=ROOT, capture_output=True, text=True,
        env={**env, "TARGET_DATABASE_URL": source},
    )
    assert same.returncode == 1
    assert "same database" in same.stderr


def test_postgres_delivery_and_callback_state(databases):
    url = databases[0]
    conn = connect(url)
    apply_migrations(conn)
    repos = PgRepos(conn)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    assert repos.updates.claim(10) is True
    repos.commit()
    assert repos.updates.claim(10) is False
    token = "confirm-token-1"
    repos.workflows.create_confirmation(42, "delete", {"id": 1}, now + __import__("datetime").timedelta(minutes=5), token)
    repos.commit()
    first = repos.workflows.consume(token, 42, now)
    assert first["kind"] == "delete"
    repos.commit()
    assert repos.workflows.consume(token, 42, now) is None
    assert repos.workflows.consume(token, 43, now) is None
    repos.customers.insert({
        "name": "Ada", "phone": "919876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 10, "start_date": "2026-01-31",
        "payment_status": "Payment pending",
    })
    repos.commit()
    row = repos.customers.get(1)
    updated = repos.customers.set_cycle_if_start(1, row["start_date"], "2026-02-28", "Paid")
    repos.commit()
    assert updated["start_date"] == "2026-02-28"
    assert repos.customers.set_cycle_if_start(1, "2026-01-31", "2026-03-01", "Paid") is None
    state = repos.deliveries.claim(1, "2026-03-28", "days:2", now)
    repos.commit()
    assert state == "claimed"
    assert repos.deliveries.claim(1, "2026-03-28", "days:2", now) == "in_flight"
    repos.deliveries.mark_failed(1, "2026-03-28", "days:2", "telegram status 500", now)
    repos.commit()
    assert repos.deliveries.claim(1, "2026-03-28", "days:2", now) == "claimed"
    repos.deliveries.mark_sent(1, "2026-03-28", "days:2", now)
    repos.commit()
    assert repos.deliveries.claim(1, "2026-03-28", "days:2", now) == "already_sent"
    repos.close()
    assert STALE_MINUTES == 10


def test_extra_table_is_reported_not_dumped(databases):
    source = databases[0]
    conn = connect(source)
    apply_migrations(conn)
    cur = conn.cursor()
    cur.execute("CREATE TABLE extra_notes (id serial primary key, body text)")
    cur.execute(
        """INSERT INTO customers (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES ('Fixture Ada', '910000000001', 'A', 'P', 1, '2026-01-01', 'Paid')"""
    )
    conn.commit()
    cur.close()
    with pytest.raises(TransferError) as caught:
        backup(conn)
    conn.close()
    assert "extra_notes" in str(caught.value)
    assert "910000000001" not in str(caught.value)
