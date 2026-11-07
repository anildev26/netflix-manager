"""
DEPRECATED. Do not use this for the Supabase to Neon move.

It inserts every CSV row again, which creates duplicates.
Use scripts/transfer_db.py and docs/RUNBOOK.md instead.

The script now refuses to run unless RUN_LEGACY_CSV_IMPORT=1 is set, and even
then it prints only a count.
"""
import os
import sys

if os.environ.get("RUN_LEGACY_CSV_IMPORT") != "1":
    print("Refusing to run migrate_data.py. Use scripts/transfer_db.py.")
    sys.exit(2)

import csv
import psycopg2

CSV_PATH     = os.environ.get("CSV_PATH", r"C:\Users\ANIL\Downloads\netflix_customers_2026-03-16 (1).csv")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: Set DATABASE_URL to your Supabase connection string first.")
    print()
    print("  Windows:   set DATABASE_URL=postgresql://postgres:PASSWORD@db.XXXX.supabase.co:5432/postgres")
    print("  Mac/Linux: export DATABASE_URL=postgresql://postgres:PASSWORD@db.XXXX.supabase.co:5432/postgres")
    sys.exit(1)

# ── Read CSV ──────────────────────────────────────────────────────────────────
print(f"Reading {CSV_PATH} ...")
with open(CSV_PATH, encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
print(f"  Found {len(rows)} customers.")

# ── Connect to Supabase ───────────────────────────────────────────────────────
print("Connecting to Supabase ...")
conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()

# ── Create table ──────────────────────────────────────────────────────────────
print("Creating table (if not exists) ...")
cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id              SERIAL PRIMARY KEY,
        name            TEXT   NOT NULL,
        phone           TEXT   NOT NULL,
        account         TEXT   NOT NULL,
        profile_name    TEXT   NOT NULL,
        monthly_amount  REAL   NOT NULL DEFAULT 0,
        start_date      TEXT   NOT NULL,
        payment_status  TEXT   NOT NULL DEFAULT 'Payment pending',
        created_at      TEXT   NOT NULL DEFAULT TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD')
    )
""")

# ── Insert rows ───────────────────────────────────────────────────────────────
print("Inserting customers ...")
inserted = 0
for r in rows:
    cur.execute(
        """INSERT INTO customers
               (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            r["Name"].strip(),
            r["Phone"].strip(),
            r["Account"].strip(),
            r["Profile Name"].strip(),
            float(r["Monthly Amount"]),
            r["Start Date"].strip(),
            r["Payment Status"].strip(),
        ),
    )
    inserted += 1

conn.commit()
cur.close()
conn.close()

print(f"\nDone. Migrated {inserted} customers.")
