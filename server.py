import csv
import io
import os
import sqlite3
import calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, Response
import jwt

# ─── App & config ─────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="public")

DB_PATH    = os.environ.get("DB_PATH", "netflix.db")
JWT_SECRET = os.environ.get("JWT_SECRET", "change-this-secret-in-production-32chars+")

ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

TEST_USER  = os.environ.get("TEST_USERNAME", "test")
TEST_PASS  = os.environ.get("TEST_PASSWORD", "Test@Admin123")

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            phone           TEXT    NOT NULL,
            account         TEXT    NOT NULL,
            profile_name    TEXT    NOT NULL,
            monthly_amount  REAL    NOT NULL DEFAULT 0,
            start_date      TEXT    NOT NULL,
            payment_status  TEXT    NOT NULL DEFAULT 'Payment pending',
            created_at      TEXT    NOT NULL DEFAULT (date('now'))
        )
    """)
    conn.commit()
    conn.close()

# ─── Business logic ───────────────────────────────────────────────────────────

def add_one_month(d: date) -> date:
    """Add exactly one calendar month, clamping to month end if needed."""
    month = d.month + 1
    year  = d.year
    if month > 12:
        month = 1
        year += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def compute_customer(row) -> dict:
    """Attach computed fields (expiry, days_left, status) to a DB row dict."""
    c      = dict(row)
    start  = date.fromisoformat(c["start_date"])
    expiry = add_one_month(start)
    today  = date.today()
    days   = (expiry - today).days

    c["expiry_date"] = expiry.isoformat()
    c["days_left"]   = days

    if days < 0:
        c["status"] = "Expired"
    elif days == 0:
        c["status"] = "Expires today"
    elif days <= 2:
        c["status"] = "Expiring soon"
    else:
        c["status"] = "Active"

    return c

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _decode_token():
    """Decode the Bearer token from the current request. Returns payload dict or None."""
    auth  = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def token_required(f):
    """Allow any valid token (admin or test)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        payload = _decode_token()
        if payload is None:
            auth = request.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "").strip()
            if not token:
                return jsonify({"error": "Authorization token required"}), 401
            # Try to give a more specific error
            try:
                jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Session expired — please log in again"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Allow only admin role tokens — blocks test/demo users."""
    @wraps(f)
    def decorated(*args, **kwargs):
        payload = _decode_token()
        if payload is None:
            return jsonify({"error": "Authorization required"}), 401
        if payload.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    u    = data.get("username", "")
    p    = data.get("password", "")

    if u == ADMIN_USER and p == ADMIN_PASS:
        role = "admin"
    elif u == TEST_USER and p == TEST_PASS:
        role = "test"
    else:
        return jsonify({"error": "Invalid username or password"}), 401

    token = jwt.encode(
        {"sub": u, "role": role, "exp": datetime.utcnow() + timedelta(days=7)},
        JWT_SECRET,
        algorithm="HS256",
    )
    return jsonify({"token": token, "role": role})

# ─── Customer routes ──────────────────────────────────────────────────────────

@app.route("/api/customers", methods=["GET"])
@token_required
def get_customers():
    payload = _decode_token()
    role    = payload.get("role", "admin") if payload else "admin"

    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
    conn.close()

    result = [compute_customer(r) for r in rows]

    # Mask sensitive fields for test/demo users
    if role == "test":
        for c in result:
            c["phone"]          = "●●●●●●●●●●"
            c["monthly_amount"] = "●●●●"

    return jsonify(result)


@app.route("/api/customers", methods=["POST"])
@admin_required
def add_customer():
    data = request.get_json(silent=True) or {}
    required = ("name", "phone", "account", "profile_name", "monthly_amount", "start_date")
    for field in required:
        if field not in data or (data[field] == "" and field != "monthly_amount"):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO customers
               (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"].strip(),
            data["phone"].strip(),
            data["account"].strip(),
            data["profile_name"].strip(),
            float(data["monthly_amount"]),
            data["start_date"],
            data.get("payment_status", "Payment pending"),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(compute_customer(row)), 201


@app.route("/api/customers/<int:cid>", methods=["PUT"])
@admin_required
def update_customer(cid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        """UPDATE customers
           SET name=?, phone=?, account=?, profile_name=?,
               monthly_amount=?, start_date=?, payment_status=?
           WHERE id=?""",
        (
            data["name"].strip(),
            data["phone"].strip(),
            data["account"].strip(),
            data["profile_name"].strip(),
            float(data["monthly_amount"]),
            data["start_date"],
            data.get("payment_status", "Payment pending"),
            cid,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Customer not found"}), 404
    return jsonify(compute_customer(row))


@app.route("/api/customers/<int:cid>", methods=["DELETE"])
@admin_required
def delete_customer(cid):
    conn = get_db()
    conn.execute("DELETE FROM customers WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/customers/<int:cid>/paid", methods=["POST"])
@admin_required
def mark_paid(cid):
    """Renew subscription: advance start_date by 1 month, reset payment status."""
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Customer not found"}), 404

    # Always advance 1 month from the current start_date (i.e. the previous expiry).
    # This preserves the billing cycle even if the user paid late.
    new_start = add_one_month(date.fromisoformat(row["start_date"]))
    conn.execute(
        "UPDATE customers SET start_date=?, payment_status=? WHERE id=?",
        (new_start.isoformat(), "Paid", cid),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    conn.close()
    return jsonify(compute_customer(row))

# ─── Import / Export ──────────────────────────────────────────────────────────

CSV_HEADERS = ["Name", "Phone", "Account", "Profile Name", "Monthly Amount", "Start Date", "Payment Status"]
CSV_FIELDS  = ["name", "phone", "account", "profile_name", "monthly_amount", "start_date", "payment_status"]


@app.route("/api/customers/export", methods=["GET"])
@admin_required
def export_customers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    for r in rows:
        writer.writerow([r["name"], r["phone"], r["account"], r["profile_name"],
                         r["monthly_amount"], r["start_date"], r["payment_status"]])

    today = date.today().isoformat()
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=netflix_customers_{today}.csv"},
    )


@app.route("/api/customers/import", methods=["POST"])
@admin_required
def import_customers():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    try:
        content = file.read().decode("utf-8-sig")   # utf-8-sig strips Excel BOM
        reader  = csv.DictReader(io.StringIO(content))
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    added  = 0
    errors = []

    conn = get_db()
    try:
        for row_num, raw in enumerate(reader, start=2):
            # Normalize keys: strip whitespace, lower-case for flexible matching
            row = {k.strip().lower(): v.strip() for k, v in raw.items() if k}

            def get(*keys):
                for k in keys:
                    if k.lower() in row and row[k.lower()]:
                        return row[k.lower()]
                return ""

            name           = get("name")
            phone          = get("phone", "whatsapp")
            account        = get("account", "netflix account")
            profile_name   = get("profile name", "profile_name", "profile")
            amount_raw     = get("monthly amount", "monthly_amount", "amount") or "0"
            start_date_raw = get("start date", "start_date")
            payment_status = get("payment status", "payment_status") or "Payment pending"

            row_errors = []
            if not name:           row_errors.append("Name is required")
            if not phone:          row_errors.append("Phone is required")
            if not account:        row_errors.append("Account is required")
            if not profile_name:   row_errors.append("Profile Name is required")
            if not start_date_raw: row_errors.append("Start Date is required")

            try:
                amount = float(amount_raw)
            except ValueError:
                row_errors.append(f"Invalid amount '{amount_raw}'")
                amount = 0.0

            if start_date_raw:
                try:
                    date.fromisoformat(start_date_raw)
                except ValueError:
                    row_errors.append(f"Invalid date '{start_date_raw}' — use YYYY-MM-DD")

            if row_errors:
                errors.append(f"Row {row_num}: {'; '.join(row_errors)}")
                continue

            conn.execute(
                """INSERT INTO customers
                       (name, phone, account, profile_name, monthly_amount, start_date, payment_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, phone, account, profile_name, amount, start_date_raw, payment_status),
            )
            added += 1

        conn.commit()
    finally:
        conn.close()

    return jsonify({"added": added, "errors": errors})


# ─── Serve frontend ────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and os.path.exists(os.path.join("public", path)):
        return send_from_directory("public", path)
    return send_from_directory("public", "index.html")

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port  = int(os.environ.get("PORT", 3000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"\n  Netflix Manager running at http://localhost:{port}")
    print(f"  Admin : {ADMIN_USER}")
    print(f"  Test  : {TEST_USER}")
    print(f"  Using custom JWT secret: {'YES' if JWT_SECRET != 'change-this-secret-in-production-32chars+' else 'NO (using default!)'}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
