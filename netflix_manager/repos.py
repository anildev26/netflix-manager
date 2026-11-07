"""Customer, reminder, and bot persistence.

MemoryRepos is for tests. PgRepos talks to Postgres and is what the app uses.
Neither implementation runs schema DDL.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras

CUSTOMER_FIELDS = (
    "name", "phone", "account", "profile_name",
    "monthly_amount", "start_date", "payment_status",
)
PARTIAL_FIELDS = set(CUSTOMER_FIELDS)
STALE_MINUTES = 10

CLAIM_SQL = """
INSERT INTO reminder_deliveries (
    customer_id, expiry_date, milestone, status, attempt_count
) VALUES (%s, %s, %s, 'sending', 1)
ON CONFLICT (customer_id, expiry_date, milestone)
DO UPDATE SET
    status = 'sending',
    attempt_count = reminder_deliveries.attempt_count + 1,
    last_error = NULL,
    updated_at = NOW()
WHERE reminder_deliveries.status = 'failed'
   OR (
        reminder_deliveries.status = 'sending'
        AND reminder_deliveries.updated_at < NOW() - (%s * INTERVAL '1 minute')
   )
RETURNING id
"""


class SchemaNotReady(RuntimeError):
    pass


def _copy_row(row: dict) -> dict:
    return dict(row)


class MemoryRepos:
    def __init__(self):
        self.customers = MemoryCustomers()
        self.deliveries = MemoryDeliveries()
        self.workflows = MemoryWorkflows()
        self.updates = MemoryUpdates()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class MemoryCustomers:
    def __init__(self):
        self.rows = {}
        self.seq = 1
        self.calls = 0

    def list_all(self):
        self.calls += 1
        return [_copy_row(self.rows[key]) for key in sorted(self.rows)]

    def get(self, cid):
        self.calls += 1
        row = self.rows.get(cid)
        return _copy_row(row) if row else None

    def insert(self, fields):
        self.calls += 1
        cid = self.seq
        self.seq += 1
        row = {key: fields[key] for key in CUSTOMER_FIELDS}
        row["id"] = cid
        row["monthly_amount"] = float(fields["monthly_amount"])
        row["created_at"] = date.today().isoformat()
        self.rows[cid] = row
        return _copy_row(row)

    def update(self, cid, fields):
        self.calls += 1
        if cid not in self.rows:
            return None
        for key in CUSTOMER_FIELDS:
            self.rows[cid][key] = fields[key]
        self.rows[cid]["monthly_amount"] = float(fields["monthly_amount"])
        return _copy_row(self.rows[cid])

    def update_partial(self, cid, changes):
        self.calls += 1
        unknown = set(changes) - PARTIAL_FIELDS
        if unknown or not changes or cid not in self.rows:
            return None
        for key, value in changes.items():
            self.rows[cid][key] = float(value) if key == "monthly_amount" else value
        return _copy_row(self.rows[cid])

    def delete(self, cid):
        self.calls += 1
        return self.rows.pop(cid, None) is not None

    def set_cycle(self, cid, start_date, payment_status):
        self.calls += 1
        if cid not in self.rows:
            return None
        self.rows[cid]["start_date"] = start_date
        self.rows[cid]["payment_status"] = payment_status
        return _copy_row(self.rows[cid])

    def set_cycle_if_start(self, cid, expected, start_date, payment_status):
        self.calls += 1
        row = self.rows.get(cid)
        if not row or row["start_date"] != expected:
            return None
        row["start_date"] = start_date
        row["payment_status"] = payment_status
        return _copy_row(row)


class MemoryDeliveries:
    def __init__(self):
        self.rows = {}

    def claim(self, customer_id, expiry_date, milestone, now):
        key = (customer_id, expiry_date, milestone)
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = {"status": "sending", "attempts": 1, "updated": now, "error": None}
            return "claimed"
        if row["status"] == "sent":
            return "already_sent"
        age = now - row["updated"]
        if row["status"] == "sending" and age < timedelta(minutes=STALE_MINUTES):
            return "in_flight"
        row["status"] = "sending"
        row["attempts"] += 1
        row["updated"] = now
        row["error"] = None
        return "claimed"

    def mark_sent(self, customer_id, expiry_date, milestone, now):
        row = self.rows[(customer_id, expiry_date, milestone)]
        row["status"] = "sent"
        row["updated"] = now
        row["error"] = None

    def mark_failed(self, customer_id, expiry_date, milestone, error, now):
        row = self.rows[(customer_id, expiry_date, milestone)]
        row["status"] = "failed"
        row["updated"] = now
        row["error"] = error


class MemoryWorkflows:
    def __init__(self):
        self.rows = {}
        self.seq = 0

    def save_wizard(self, owner_id, payload, expires_at, token):
        self.clear_wizard(owner_id)
        self.rows[token] = {
            "id": token,
            "owner_id": owner_id,
            "kind": "wizard",
            "payload": payload,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return token

    def active_wizard(self, owner_id, now):
        for row in self.rows.values():
            if row["owner_id"] == owner_id and row["kind"] == "wizard" and row["consumed_at"] is None and row["expires_at"] > now:
                found = dict(row)
                found["payload"] = dict(row["payload"])
                return found
        return None

    def clear_wizard(self, owner_id):
        for key, row in list(self.rows.items()):
            if row["owner_id"] == owner_id and row["kind"] == "wizard" and row["consumed_at"] is None:
                del self.rows[key]

    def create_confirmation(self, owner_id, kind, payload, expires_at, token):
        self.rows[token] = {
            "id": token,
            "owner_id": owner_id,
            "kind": kind,
            "payload": payload,
            "expires_at": expires_at,
            "consumed_at": None,
        }
        return token

    def consume(self, token, owner_id, now):
        row = self.rows.get(token)
        if not row or row["consumed_at"] is not None:
            return None
        if row["owner_id"] != owner_id or row["expires_at"] <= now:
            return None
        saved = {"kind": row["kind"], "payload": row["payload"]}
        row["consumed_at"] = now
        row["payload"] = {}
        return saved


class MemoryUpdates:
    def __init__(self):
        self.ids = set()

    def claim(self, update_id):
        if update_id in self.ids:
            return False
        self.ids.add(update_id)
        return True

    def release(self, update_id):
        self.ids.discard(update_id)


class PgRepos:
    def __init__(self, conn):
        self.conn = conn
        self.customers = PgCustomers(conn)
        self.deliveries = PgDeliveries(conn)
        self.workflows = PgWorkflows(conn)
        self.updates = PgUpdates(conn)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def _pgcode_guard(exc):
    if getattr(exc, "pgcode", None) == "42P01":
        raise SchemaNotReady() from None
    raise


class _Pg:
    def __init__(self, conn):
        self.conn = conn

    def _run(self, sql, params=None, fetch="all"):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(sql, params)
            if fetch == "all":
                return [dict(row) for row in cur.fetchall()]
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row else None
            if fetch == "rowcount":
                return cur.rowcount
            return None
        except SchemaNotReady:
            raise
        except Exception as exc:
            _pgcode_guard(exc)
        finally:
            cur.close()


class PgCustomers(_Pg):
    def list_all(self):
        return self._run("SELECT * FROM customers ORDER BY id")

    def get(self, cid):
        return self._run("SELECT * FROM customers WHERE id = %s", (cid,), fetch="one")

    def insert(self, fields):
        return self._run(
            """INSERT INTO customers
                   (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            tuple(fields[key] for key in CUSTOMER_FIELDS),
            fetch="one",
        )

    def update(self, cid, fields):
        count = self._run(
            """UPDATE customers
               SET name=%s, phone=%s, account=%s, profile_name=%s,
                   monthly_amount=%s, start_date=%s, payment_status=%s
               WHERE id=%s""",
            tuple(fields[key] for key in CUSTOMER_FIELDS) + (cid,),
            fetch="rowcount",
        )
        if count != 1:
            return None
        return self.get(cid)

    def update_partial(self, cid, changes):
        unknown = set(changes) - PARTIAL_FIELDS
        if unknown or not changes:
            return None
        keys = [key for key in CUSTOMER_FIELDS if key in changes]
        assignments = ", ".join(f"{key}=%s" for key in keys)
        params = tuple(changes[key] for key in keys) + (cid,)
        count = self._run(
            f"UPDATE customers SET {assignments} WHERE id=%s",
            params,
            fetch="rowcount",
        )
        if count != 1:
            return None
        return self.get(cid)

    def delete(self, cid):
        count = self._run("DELETE FROM customers WHERE id = %s", (cid,), fetch="rowcount")
        return count == 1

    def set_cycle(self, cid, start_date, payment_status):
        self._run(
            "UPDATE customers SET start_date=%s, payment_status=%s WHERE id=%s",
            (start_date, payment_status, cid),
            fetch="none",
        )
        return self.get(cid)

    def set_cycle_if_start(self, cid, expected, start_date, payment_status):
        count = self._run(
            """UPDATE customers
               SET start_date=%s, payment_status=%s
               WHERE id=%s AND start_date=%s""",
            (start_date, payment_status, cid, expected),
            fetch="rowcount",
        )
        if count != 1:
            return None
        return self.get(cid)


class PgDeliveries(_Pg):
    def claim(self, customer_id, expiry_date, milestone, now):
        del now  # Postgres uses its own clock for the stale-lock interval.
        row = self._run(
            CLAIM_SQL,
            (customer_id, expiry_date, milestone, STALE_MINUTES),
            fetch="one",
        )
        if row:
            return "claimed"
        existing = self._run(
            """SELECT status FROM reminder_deliveries
               WHERE customer_id=%s AND expiry_date=%s AND milestone=%s""",
            (customer_id, expiry_date, milestone),
            fetch="one",
        )
        if existing and existing["status"] == "sent":
            return "already_sent"
        return "in_flight"

    def mark_sent(self, customer_id, expiry_date, milestone, now):
        del now
        self._run(
            """UPDATE reminder_deliveries
               SET status='sent', last_error=NULL, updated_at=NOW()
               WHERE customer_id=%s AND expiry_date=%s AND milestone=%s""",
            (customer_id, expiry_date, milestone),
            fetch="none",
        )

    def mark_failed(self, customer_id, expiry_date, milestone, error, now):
        del now
        self._run(
            """UPDATE reminder_deliveries
               SET status='failed', last_error=%s, updated_at=NOW()
               WHERE customer_id=%s AND expiry_date=%s AND milestone=%s""",
            (error, customer_id, expiry_date, milestone),
            fetch="none",
        )


class PgWorkflows(_Pg):
    def save_wizard(self, owner_id, payload, expires_at, token):
        self.clear_wizard(owner_id)
        self._run(
            """INSERT INTO telegram_workflows (id, owner_id, kind, payload, expires_at)
               VALUES (%s, %s, 'wizard', %s, %s)""",
            (token, owner_id, json.dumps(payload), expires_at),
            fetch="none",
        )
        return token

    def active_wizard(self, owner_id, now):
        row = self._run(
            """SELECT id, owner_id, kind, payload, expires_at
               FROM telegram_workflows
               WHERE owner_id=%s AND kind='wizard' AND consumed_at IS NULL AND expires_at > %s
               ORDER BY created_at DESC
               LIMIT 1""",
            (owner_id, now),
            fetch="one",
        )
        if not row:
            return None
        row["payload"] = json.loads(row["payload"])
        return row

    def clear_wizard(self, owner_id):
        self._run(
            """DELETE FROM telegram_workflows
               WHERE owner_id=%s AND kind='wizard' AND consumed_at IS NULL""",
            (owner_id,),
            fetch="none",
        )

    def create_confirmation(self, owner_id, kind, payload, expires_at, token):
        self._run(
            """INSERT INTO telegram_workflows (id, owner_id, kind, payload, expires_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (token, owner_id, kind, json.dumps(payload), expires_at),
            fetch="none",
        )
        return token

    def consume(self, token, owner_id, now):
        row = self._run(
            """UPDATE telegram_workflows
               SET consumed_at=%s
               WHERE id=%s AND owner_id=%s AND consumed_at IS NULL AND expires_at > %s
               RETURNING kind, payload""",
            (now, token, owner_id, now),
            fetch="one",
        )
        if not row:
            return None
        self._run(
            "UPDATE telegram_workflows SET payload='{}' WHERE id=%s",
            (token,),
            fetch="none",
        )
        row["payload"] = json.loads(row["payload"])
        return row


class PgUpdates(_Pg):
    def claim(self, update_id):
        row = self._run(
            """INSERT INTO telegram_updates (update_id) VALUES (%s)
               ON CONFLICT DO NOTHING
               RETURNING update_id""",
            (update_id,),
            fetch="one",
        )
        return row is not None

    def release(self, update_id):
        self._run(
            "DELETE FROM telegram_updates WHERE update_id=%s",
            (update_id,),
            fetch="none",
        )


def utcnow():
    return datetime.now(timezone.utc)
