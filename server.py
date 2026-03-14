import os
import sqlite3
import calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory
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

    # If still active: push forward 1 month from current start.
    # If already expired: start fresh from today so they get a full month.
    advanced  = add_one_month(date.fromisoformat(row["start_date"]))
    new_start = advanced if advanced >= date.today() else date.today()
    conn.execute(
        "UPDATE customers SET start_date=?, payment_status=? WHERE id=?",
        (new_start.isoformat(), "Payment pending", cid),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    conn.close()
    return jsonify(compute_customer(row))

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
