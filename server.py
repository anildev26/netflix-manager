import os
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import jwt
import psycopg2
from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from netflix_manager.bot import BotContext, handle_update
from netflix_manager.config import (
    auth_material,
    auth_problems,
    const_eq,
    cron_authorized,
    default_country_code,
    demo_credentials,
    dry_run_enabled,
    load_bot_settings,
    milestone_days,
    reminder_gate,
    setup_token,
    telegram_chat_id,
    telegram_token,
)
from netflix_manager.csvio import export_csv, plan_import
from netflix_manager.dates import add_one_month, compute_customer, today_ist
from netflix_manager.messages import InvalidTemplate, message_template as load_template, renewal_link
from netflix_manager.repos import PgRepos, SchemaNotReady
from netflix_manager.reminders import run_reminders
from netflix_manager.schema import customers_ddl
from netflix_manager.telegram_api import TelegramError, fetch_file, send_message

_base_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(_base_dir, "public"))
app.config["PROPAGATE_EXCEPTIONS"] = False


def get_db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or url == "memory":
        raise RuntimeError("database is not configured")
    return psycopg2.connect(url)


def get_repos():
    factory = app.config.get("REPOS_FACTORY")
    if factory:
        return factory()
    return PgRepos(get_db())


def init_db():
    """Create the customers table only. Never called by ordinary requests."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(customers_ddl())
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _decode_token():
    material = auth_material()
    if not material:
        return None
    header = request.headers.get("Authorization", "")
    token = header.replace("Bearer ", "", 1).strip()
    if not token:
        return None
    try:
        return jwt.decode(token, material[2], algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


def _unauthorized():
    header = request.headers.get("Authorization", "")
    token = header.replace("Bearer ", "", 1).strip()
    if not token:
        return jsonify({"error": "Authorization token required"}), 401
    material = auth_material()
    if not material:
        return jsonify({"error": "Authentication is not configured"}), 503
    try:
        jwt.decode(token, material[2], algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Session expired — please log in again"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "Invalid token"}), 401
    return jsonify({"error": "Invalid token"}), 401


def token_required(func):
    @wraps(func)
    def decorated(*args, **kwargs):
        if auth_problems():
            return jsonify({"error": "Authentication is not configured"}), 503
        payload = _decode_token()
        if payload is None:
            return _unauthorized()
        return func(*args, **kwargs)
    return decorated


def admin_required(func):
    @wraps(func)
    def decorated(*args, **kwargs):
        if auth_problems():
            return jsonify({"error": "Authentication is not configured"}), 503
        payload = _decode_token()
        if payload is None:
            return _unauthorized()
        if payload.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return func(*args, **kwargs)
    return decorated


def _role():
    payload = _decode_token() or {}
    role = payload.get("role")
    if role in {"admin", "test"}:
        return role
    return None


def demo_rows(today: date):
    start = (today - timedelta(days=3)).isoformat()
    return [{
        "id": 1,
        "name": "Demo Customer",
        "phone": "910000000000",
        "account": "Demo Account",
        "profile_name": "Demo Profile",
        "monthly_amount": 0,
        "start_date": start,
        "payment_status": "Payment pending",
        "created_at": today.isoformat(),
    }]


def _mask_demo(rows):
    masked = []
    for row in rows:
        row["phone"] = "●●●●●●●●●●"
        row["monthly_amount"] = "●●●●"
        masked.append(row)
    return masked


def _customer_fields(data):
    return {
        "name": data["name"].strip(),
        "phone": data["phone"].strip(),
        "account": data["account"].strip(),
        "profile_name": data["profile_name"].strip(),
        "monthly_amount": float(data["monthly_amount"]),
        "start_date": data["start_date"],
        "payment_status": data.get("payment_status", "Payment pending"),
    }


def _no_store(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


@app.route("/api/login", methods=["POST"])
def login():
    material = auth_material()
    if not material:
        return jsonify({"error": "Authentication is not configured"}), 503
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    admin_user, admin_password, secret = material
    if const_eq(username, admin_user) and const_eq(password, admin_password):
        role = "admin"
        subject = username
    else:
        demo = demo_credentials()
        if not demo or not (const_eq(username, demo[0]) and const_eq(password, demo[1])):
            return jsonify({"error": "Invalid username or password"}), 401
        role = "test"
        subject = username
    token = jwt.encode(
        {"sub": subject, "role": role, "exp": datetime.now(timezone.utc) + timedelta(days=7)},
        secret,
        algorithm="HS256",
    )
    return jsonify({"token": token, "role": role})


@app.route("/api/customers", methods=["GET"])
@token_required
def get_customers():
    role = _role()
    if role == "test":
        rows = demo_rows(date.today())
    elif role == "admin":
        repos = get_repos()
        try:
            rows = repos.customers.list_all()
        finally:
            repos.close()
    else:
        return jsonify({"error": "Invalid token"}), 401
    result = [compute_customer(row) for row in rows]
    if role == "test":
        result = _mask_demo(result)
    return jsonify(result)


@app.route("/api/customers", methods=["POST"])
@admin_required
def add_customer():
    data = request.get_json(silent=True) or {}
    required = ("name", "phone", "account", "profile_name", "monthly_amount", "start_date")
    for field in required:
        if field not in data or (data[field] == "" and field != "monthly_amount"):
            return jsonify({"error": f"Missing required field: {field}"}), 400
    repos = get_repos()
    try:
        row = repos.customers.insert(_customer_fields(data))
        repos.commit()
    finally:
        repos.close()
    return jsonify(compute_customer(row)), 201


@app.route("/api/customers/<int:cid>", methods=["PUT"])
@admin_required
def update_customer(cid):
    data = request.get_json(silent=True) or {}
    repos = get_repos()
    try:
        row = repos.customers.update(cid, _customer_fields(data))
        repos.commit()
    finally:
        repos.close()
    if not row:
        return jsonify({"error": "Customer not found"}), 404
    return jsonify(compute_customer(row))


@app.route("/api/customers/<int:cid>", methods=["DELETE"])
@admin_required
def delete_customer(cid):
    repos = get_repos()
    try:
        repos.customers.delete(cid)
        repos.commit()
    finally:
        repos.close()
    return jsonify({"success": True})


@app.route("/api/customers/<int:cid>/paid", methods=["POST"])
@admin_required
def mark_paid(cid):
    repos = get_repos()
    try:
        row = repos.customers.get(cid)
        if not row:
            return jsonify({"error": "Customer not found"}), 404
        new_start = add_one_month(date.fromisoformat(row["start_date"]))
        updated = repos.customers.set_cycle(cid, new_start.isoformat(), "Paid")
        repos.commit()
    finally:
        repos.close()
    return jsonify(compute_customer(updated))


@app.route("/api/customers/<int:cid>/whatsapp", methods=["GET"])
@admin_required
def customer_whatsapp(cid):
    country = default_country_code()
    repos = get_repos()
    try:
        row = repos.customers.get(cid)
    finally:
        repos.close()
    if not row:
        return jsonify({"error": "Customer not found"}), 404
    try:
        view = compute_customer(row)
        template = load_template()
    except InvalidTemplate:
        return jsonify({"error": "Reminder template is invalid"}), 500
    message, url = renewal_link(view, country, template)
    if not url:
        return jsonify({"error": "This phone number needs a valid country code before a WhatsApp link can be created."}), 422
    return jsonify({"url": url, "message": message})


@app.route("/api/customers/export", methods=["GET"])
@admin_required
def export_customers():
    repos = get_repos()
    try:
        rows = repos.customers.list_all()
    finally:
        repos.close()
    today = date.today().isoformat()
    return Response(
        export_csv(rows),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=netflix_customers_{today}.csv"},
    )


@app.route("/api/customers/import", methods=["POST"])
@admin_required
def import_customers():
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "No file provided"}), 400
    raw = upload.read(200_001)
    if len(raw) > 200_000:
        return jsonify({"error": "File is too large"}), 400
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        return jsonify({"error": "Could not read file"}), 400
    country = default_country_code()
    if country is None:
        return jsonify({"error": "Phone country code is not configured"}), 500
    repos = get_repos()
    try:
        plan = plan_import(content, repos.customers.list_all(), country)
        for fields in plan.rows:
            repos.customers.insert(fields)
        repos.commit()
    finally:
        repos.close()
    return jsonify({"added": len(plan.rows), "errors": plan.errors, "duplicates": plan.duplicates})


@app.route("/api/cron/reminders", methods=["GET", "POST"])
def cron_reminders():
    if not cron_authorized(request.headers.get("Authorization", "")):
        return _no_store({"error": "Unauthorized"}, 401)
    gate = reminder_gate()
    if gate == "disabled":
        return _no_store({
            "ok": True,
            "enabled": False,
            "dry_run": True,
            "sent": 0,
            "reason": "telegram_not_configured",
        })
    if gate == "bad_destination":
        return _no_store({"error": "Telegram destination must be the owner's private chat id"}, 500)
    milestones = milestone_days()
    if not milestones:
        return _no_store({"error": "Reminder milestones are invalid"}, 500)
    try:
        template = load_template()
    except InvalidTemplate:
        return _no_store({"error": "Reminder template is invalid"}, 500)
    country = default_country_code()
    token = telegram_token()
    chat_id = telegram_chat_id()
    repos = get_repos()
    try:
        rows = repos.customers.list_all()
        def send(text, chat_id=chat_id, token=token):
            send_message(token, chat_id, text)
        result = run_reminders(
            rows=rows,
            today=today_ist(),
            milestones=milestones,
            template=template,
            country_code=country,
            dry_run=dry_run_enabled(),
            store=repos.deliveries,
            send=send,
            now=datetime.now(timezone.utc),
            commit=repos.commit,
        )
        return _no_store(result)
    except SchemaNotReady:
        return _no_store({"error": "Reminder storage is not initialized"}, 503)
    finally:
        repos.close()


@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    settings = load_bot_settings()
    if not settings.enabled:
        return "", 404
    presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not const_eq(presented, settings.webhook_secret or ""):
        return "", 401
    raw = request.get_data(cache=True) or b""
    if len(raw) > 100_000:
        return "", 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return "", 400
    repos = get_repos()
    try:
        def fetcher(file_id, token=settings.token):
            return fetch_file(token, file_id)
        ctx = BotContext(
            settings=settings,
            repos=repos,
            outbox=_BotOutbox(settings),
            now=datetime.now(timezone.utc),
            country_code=default_country_code() or "",
            fetcher=fetcher,
        )
        handle_update(payload, ctx)
        return jsonify({"ok": True})
    except SchemaNotReady:
        return jsonify({"error": "Bot storage is not initialized"}), 503
    finally:
        repos.close()


class _BotOutbox:
    """Sends only to allowlisted private chats. The request cannot choose another chat."""

    def __init__(self, settings):
        self.settings = settings

    def send_message(self, chat_id, text, buttons=None):
        if chat_id not in self.settings.owner_ids:
            raise TelegramError("refusing destination")
        send_message(self.settings.token, chat_id, text, buttons)

    def send_document(self, chat_id, filename, data):
        if chat_id not in self.settings.owner_ids:
            raise TelegramError("refusing destination")
        from netflix_manager.telegram_api import send_document
        send_document(self.settings.token, chat_id, filename, data)

    def answer(self, callback_id, text):
        from netflix_manager.telegram_api import answer_callback
        answer_callback(self.settings.token, callback_id, text)


@app.route("/api/setup", methods=["POST"])
def setup():
    token = setup_token()
    if not token:
        return jsonify({"error": "Not found"}), 404
    presented = request.headers.get("X-Setup-Token", "")
    if not const_eq(presented, token):
        return jsonify({"error": "Unauthorized"}), 401
    init_db()
    return jsonify({"ok": True})


@app.errorhandler(Exception)
def handle_exception(exc):
    if isinstance(exc, HTTPException):
        if exc.code and exc.code >= 500:
            return jsonify({"error": "Internal server error"}), exc.code
        return jsonify({"error": exc.description or "Error"}), exc.code
    app.logger.error("request failed (%s)", type(exc).__name__)
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(404)
def handle_404(_exc):
    return jsonify({"error": "Not found"}), 404


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    from flask import send_from_directory
    public = os.path.realpath(os.path.join(_base_dir, "public"))
    if path:
        target = os.path.realpath(os.path.join(public, path))
        if target.startswith(public + os.sep) and os.path.isfile(target):
            return send_from_directory(public, os.path.relpath(target, public))
    return send_from_directory(public, "index.html")


if __name__ == "__main__":
    if auth_problems():
        print("Authentication is not configured. Refusing to start.")
        raise SystemExit(1)
    init_db()
    port = int(os.environ.get("PORT", "3000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"Netflix Manager running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
