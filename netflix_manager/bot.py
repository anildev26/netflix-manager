"""Private owner commands. State lives in the database so Vercel instances stay stateless."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime

from netflix_manager.csvio import MAX_BYTES, export_csv, identity_key, plan_import
from netflix_manager.dates import add_one_month, compute_customer, format_amount, today_ist
from netflix_manager.phones import normalize_phone
from netflix_manager.repos import SchemaNotReady
from netflix_manager.telegram_api import TelegramError

PAGE_SIZE = 5
FILTERS = {"all": "all", "act": "active", "exp": "expiring", "expd": "expired"}
FILTER_LABELS = {
    "all": "All",
    "active": "Active",
    "expiring": "Expiring soon",
    "expired": "Expired",
}
EDIT_FIELDS = {
    "name": "name",
    "phone": "phone",
    "account": "account",
    "profile": "profile_name",
    "amount": "monthly_amount",
    "start": "start_date",
    "pay": "payment_status",
}
ADD_STEPS = ("name", "phone", "account", "profile", "amount", "start", "pay")
PAY_WORDS = {"paid": "Paid", "pending": "Payment pending"}
CALLBACK = re.compile(
    r"^(?:L:(all|act|exp|expd):\d{1,4}"
    r"|V:\d{1,9}"
    r"|W:(renew|delete):\d{1,9}"
    r"|P:\d{1,9}:(paid|pending)"
    r"|E:\d{1,9}:(name|phone|account|profile|amount|start|pay)"
    r"|[yn]:[A-Za-z0-9_-]{8,64})$"
)


@dataclass
class BotContext:
    settings: object
    repos: object
    outbox: object
    now: datetime
    country_code: str
    fetcher: object

    @property
    def today(self):
        return today_ist(self.now)


def new_token() -> str:
    return secrets.token_urlsafe(12)


def actor_from(update: dict) -> dict:
    if "callback_query" in update:
        query = update["callback_query"] or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        return {
            "user_id": user.get("id"),
            "chat_id": chat.get("id"),
            "chat_type": chat.get("type"),
            "callback_id": query.get("id") or "",
            "data": query.get("data") or "",
        }
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    return {
        "user_id": user.get("id"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "text": message.get("text") or "",
        "document": message.get("document"),
    }


def is_owner(actor: dict, settings) -> bool:
    user_id = actor.get("user_id")
    chat_id = actor.get("chat_id")
    if actor.get("chat_type") != "private":
        return False
    if type(user_id) is not int or type(chat_id) is not int:
        return False
    if user_id <= 0 or chat_id <= 0 or user_id != chat_id:
        return False
    return user_id in settings.owner_ids


def handle_update(update: dict, ctx) -> str:
    if not isinstance(update, dict):
        return "ignored"
    update_id = update.get("update_id")
    if type(update_id) is not int:
        return "ignored"
    actor = actor_from(update)
    if not is_owner(actor, ctx.settings):
        return "ignored"
    if not ctx.repos.updates.claim(update_id):
        ctx.repos.commit()
        return "duplicate"
    ctx.repos.commit()
    try:
        if "callback_query" in update:
            handle_callback(actor, ctx)
        else:
            handle_message(actor, update.get("message") or {}, ctx)
        ctx.repos.commit()
        return "ok"
    except SchemaNotReady:
        ctx.repos.rollback()
        raise
    except Exception:
        ctx.repos.rollback()
        ctx.outbox.send_message(
            actor["chat_id"],
            "Something went wrong. If you were confirming an action, open the record and try again.",
        )
        return "error"


def handle_message(actor, message, ctx):
    wizard = ctx.repos.workflows.active_wizard(actor["user_id"], ctx.now)
    text = (message.get("text") or "").strip()
    if text.startswith("/"):
        ctx.repos.workflows.clear_wizard(actor["user_id"])
        ctx.repos.commit()
        run_command(actor, text, ctx)
        return
    if wizard and wizard["payload"].get("flow") == "import":
        receive_import(actor, message, ctx)
        return
    if wizard and wizard["payload"].get("flow") in {"add", "edit"}:
        continue_wizard(actor, wizard["payload"], text, ctx)
        return
    say(ctx, actor, "Send /help to see owner commands.")


def run_command(actor, text, ctx):
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    arg = arg.strip()
    if command in {"/start", "/help"}:
        say(ctx, actor, HELP)
    elif command == "/cancel":
        say(ctx, actor, "Cancelled.")
    elif command == "/list":
        choice = {"active": "active", "expiring": "expiring", "expired": "expired"}.get(arg.lower(), "all")
        send_list(actor, ctx, choice, 0)
    elif command == "/find":
        send_search(actor, ctx, arg)
    elif command == "/show":
        cid = parse_id(arg)
        if cid is None:
            say(ctx, actor, "Use /show and the numeric id, for example /show 12.")
        else:
            show_customer(actor, ctx, cid)
    elif command == "/add":
        start_add(actor, ctx)
    elif command == "/edit":
        cid = parse_id(arg)
        if cid is None:
            say(ctx, actor, "Use /edit and the numeric id, for example /edit 12.")
        else:
            show_customer(actor, ctx, cid)
    elif command == "/renew":
        cid = parse_id(arg)
        if cid is None:
            say(ctx, actor, "Use /renew and the numeric id.")
        else:
            ask_renew(actor, ctx, cid)
    elif command == "/delete":
        cid = parse_id(arg)
        if cid is None:
            say(ctx, actor, "Use /delete and the numeric id.")
        else:
            ask_delete(actor, ctx, cid)
    elif command == "/pay":
        parts = arg.split()
        if len(parts) != 2 or parse_id(parts[0]) is None or parts[1].lower() not in PAY_WORDS:
            say(ctx, actor, "Use /pay ID paid or /pay ID pending. This does not change the renewal date.")
        else:
            ask_pay(actor, ctx, int(parts[0]), PAY_WORDS[parts[1].lower()])
    elif command == "/export":
        send_export(actor, ctx)
    elif command == "/import":
        start_import(actor, ctx)
    else:
        say(ctx, actor, "Unknown command. Send /help.")


def handle_callback(actor, ctx):
    data = actor.get("data") or ""
    ctx.outbox.answer(actor.get("callback_id") or "", "OK")
    if not CALLBACK.fullmatch(data):
        say(ctx, actor, "That button is not valid.")
        return
    if data.startswith("L:"):
        _, code, page = data.split(":")
        send_list(actor, ctx, FILTERS[code], int(page))
    elif data.startswith("V:"):
        show_customer(actor, ctx, int(data.split(":")[1]))
    elif data.startswith("W:renew:"):
        ask_renew(actor, ctx, int(data.split(":")[2]))
    elif data.startswith("W:delete:"):
        ask_delete(actor, ctx, int(data.split(":")[2]))
    elif data.startswith("P:"):
        _, cid, word = data.split(":")
        ask_pay(actor, ctx, int(cid), PAY_WORDS[word])
    elif data.startswith("E:"):
        _, cid, field = data.split(":")
        start_edit(actor, ctx, int(cid), field)
    elif data.startswith("y:"):
        execute_confirmation(actor, ctx, data[2:])
    elif data.startswith("n:"):
        ctx.repos.workflows.consume(data[2:], actor["user_id"], ctx.now)
        ctx.repos.commit()
        say(ctx, actor, "Cancelled. Nothing was changed.")


def send_list(actor, ctx, filt, page):
    views = filtered_views(ctx, filt)
    page = max(0, page)
    start = page * PAGE_SIZE
    chunk = views[start:start + PAGE_SIZE]
    lines = [f"{FILTER_LABELS[filt]} ({len(views)})"]
    if not chunk:
        lines.append("No customers in this filter.")
    for view in chunk:
        lines.append(f"#{view['id']} {clip(view['name'])} — {view['status']} — {view['days_left']}d — {clip(view['account'])}")
    buttons = []
    nav = []
    code = {value: key for key, value in FILTERS.items()}[filt]
    if page > 0:
        nav.append({"text": "Prev", "callback_data": f"L:{code}:{page - 1}"})
    if start + PAGE_SIZE < len(views):
        nav.append({"text": "Next", "callback_data": f"L:{code}:{page + 1}"})
    if nav:
        buttons.append(nav)
    buttons.append([
        {"text": "All", "callback_data": "L:all:0"},
        {"text": "Active", "callback_data": "L:act:0"},
        {"text": "Expiring", "callback_data": "L:exp:0"},
        {"text": "Expired", "callback_data": "L:expd:0"},
    ])
    if chunk:
        buttons.append([
            {"text": f"Open #{view['id']}", "callback_data": f"V:{view['id']}"}
            for view in chunk[:3]
        ])
    say(ctx, actor, "\n".join(lines), buttons)


def send_search(actor, ctx, query):
    cleaned = query.strip()
    if not cleaned or len(cleaned) > 80:
        say(ctx, actor, "Use /find followed by a name, phone, or account.")
        return
    needle = cleaned.casefold()
    matches = []
    for row in ctx.repos.customers.list_all():
        hay = f"{row['name']} {row['phone']} {row['account']}".casefold()
        if needle in hay:
            try:
                matches.append(compute_customer(row, today=ctx.today))
            except (TypeError, ValueError, KeyError):
                continue
    matches.sort(key=lambda view: (view["days_left"], view["id"]))
    if not matches:
        say(ctx, actor, "No customers matched.")
        return
    lines = [f"Matches ({len(matches)})"]
    for view in matches[:PAGE_SIZE]:
        lines.append(f"#{view['id']} {clip(view['name'])} — {view['status']} — {clip(view['account'])}")
    say(ctx, actor, "\n".join(lines))


def show_customer(actor, ctx, cid):
    row = ctx.repos.customers.get(cid)
    if not row:
        say(ctx, actor, f"No customer #{cid}.")
        return
    try:
        view = compute_customer(row, today=ctx.today)
    except (TypeError, ValueError, KeyError):
        say(ctx, actor, f"Customer #{cid} has a start date that cannot be read.")
        return
    text = "\n".join([
        f"#{view['id']} {view['name']}",
        f"Phone: {view['phone']}",
        f"Account: {view['account']}",
        f"Profile: {view['profile_name']}",
        f"Amount: ₹{format_amount(view['monthly_amount'])}",
        f"Start: {view['start_date']}",
        f"Expiry: {view['expiry_date']} ({view['days_left']} days)",
        f"Status: {view['status']}",
        f"Payment: {view['payment_status']}",
    ])
    buttons = [
        [{"text": "Renew", "callback_data": f"W:renew:{cid}"},
         {"text": "Delete", "callback_data": f"W:delete:{cid}"}],
        [{"text": "Mark paid", "callback_data": f"P:{cid}:paid"},
         {"text": "Mark pending", "callback_data": f"P:{cid}:pending"}],
        [{"text": "Edit name", "callback_data": f"E:{cid}:name"},
         {"text": "Edit phone", "callback_data": f"E:{cid}:phone"}],
        [{"text": "Edit account", "callback_data": f"E:{cid}:account"},
         {"text": "Edit profile", "callback_data": f"E:{cid}:profile"}],
        [{"text": "Edit amount", "callback_data": f"E:{cid}:amount"},
         {"text": "Edit start", "callback_data": f"E:{cid}:start"}],
    ]
    say(ctx, actor, text, buttons)


def start_add(actor, ctx):
    ctx.repos.workflows.save_wizard(
        actor["user_id"],
        {"flow": "add", "step": "name", "fields": {}},
        ctx.now + ctx.settings.confirm_ttl,
        new_token(),
    )
    ctx.repos.commit()
    say(ctx, actor, "New customer. Send the customer name. /cancel stops.")


def start_edit(actor, ctx, cid, field):
    row = ctx.repos.customers.get(cid)
    if not row:
        say(ctx, actor, f"No customer #{cid}.")
        return
    ctx.repos.workflows.save_wizard(
        actor["user_id"],
        {"flow": "edit", "id": cid, "field": field},
        ctx.now + ctx.settings.confirm_ttl,
        new_token(),
    )
    ctx.repos.commit()
    say(ctx, actor, f"Send the new {field} for #{cid} {clip(row['name'])}. /cancel stops.")


def continue_wizard(actor, payload, text, ctx):
    if not text:
        say(ctx, actor, "Send a value, or /cancel.")
        return
    if payload["flow"] == "add":
        step = payload["step"]
        value, problem = validate_field(step, text, ctx.country_code)
        if problem:
            say(ctx, actor, problem)
            return
        fields = dict(payload["fields"])
        fields[step] = value
        index = ADD_STEPS.index(step)
        if index + 1 < len(ADD_STEPS):
            nxt = ADD_STEPS[index + 1]
            ctx.repos.workflows.save_wizard(
                actor["user_id"],
                {"flow": "add", "step": nxt, "fields": fields},
                ctx.now + ctx.settings.confirm_ttl,
                new_token(),
            )
            ctx.repos.commit()
            say(ctx, actor, prompt_for(nxt))
            return
        ctx.repos.workflows.clear_wizard(actor["user_id"])
        preview = compute_customer({**stored_fields(fields), "id": 0}, today=ctx.today)
        ask_confirm(
            actor, ctx, "add", {"fields": stored_fields(fields)},
            "Add this customer?\n" + summary_lines(preview) + "\nNothing is saved until you confirm.",
        )
        return
    field = payload["field"]
    value, problem = validate_field(field, text, ctx.country_code)
    if problem:
        say(ctx, actor, problem)
        return
    ctx.repos.workflows.clear_wizard(actor["user_id"])
    column = EDIT_FIELDS[field]
    ask_confirm(
        actor, ctx, "edit",
        {"id": payload["id"], "changes": {column: value}},
        f"Update #{payload['id']} {field}?\nNew value: {value}\nOther fields stay the same.",
    )


def ask_renew(actor, ctx, cid):
    row = ctx.repos.customers.get(cid)
    if not row:
        say(ctx, actor, f"No customer #{cid}.")
        return
    try:
        view = compute_customer(row, today=ctx.today)
        new_start = add_one_month(date.fromisoformat(row["start_date"]))
        new_expiry = add_one_month(new_start)
    except (TypeError, ValueError, KeyError):
        say(ctx, actor, f"Customer #{cid} has a start date that cannot be renewed.")
        return
    ask_confirm(
        actor, ctx, "renew",
        {"id": cid, "expected_start": row["start_date"]},
        (
            f"Renew #{cid} {clip(row['name'])}?\n"
            f"Current expiry {view['expiry_date']}.\n"
            f"New start {new_start.isoformat()}, new expiry {new_expiry.isoformat()}.\n"
            "The old billing cycle is kept. A late payment does not start from today."
        ),
    )


def ask_delete(actor, ctx, cid):
    row = ctx.repos.customers.get(cid)
    if not row:
        say(ctx, actor, f"No customer #{cid}.")
        return
    ask_confirm(
        actor, ctx, "delete", {"id": cid},
        f"Delete #{cid} {clip(row['name'])}? This cannot be undone.",
    )


def ask_pay(actor, ctx, cid, status):
    row = ctx.repos.customers.get(cid)
    if not row:
        say(ctx, actor, f"No customer #{cid}.")
        return
    ask_confirm(
        actor, ctx, "pay",
        {"id": cid, "payment_status": status},
        f"Set payment for #{cid} {clip(row['name'])} to {status}? The renewal date will not change.",
    )


def start_import(actor, ctx):
    ctx.repos.workflows.save_wizard(
        actor["user_id"],
        {"flow": "import"},
        ctx.now + ctx.settings.confirm_ttl,
        new_token(),
    )
    ctx.repos.commit()
    say(ctx, actor, "Send a CSV file. Existing customers are not deleted or overwritten. /cancel stops.")


def receive_import(actor, message, ctx):
    document = message.get("document") or {}
    file_name = str(document.get("file_name") or "")
    file_size = document.get("file_size") or 0
    if not document.get("file_id") or not file_name.lower().endswith(".csv"):
        say(ctx, actor, "Send a .csv file, or /cancel.")
        return
    if file_size and int(file_size) > MAX_BYTES:
        say(ctx, actor, "That file is too large.")
        return
    try:
        raw = ctx.fetcher(document["file_id"])
    except TelegramError:
        say(ctx, actor, "The file could not be read.")
        return
    if len(raw) > MAX_BYTES:
        say(ctx, actor, "That file is too large.")
        return
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        say(ctx, actor, "The file must be UTF-8 CSV.")
        return
    plan = plan_import(content, ctx.repos.customers.list_all(), ctx.country_code)
    ctx.repos.workflows.clear_wizard(actor["user_id"])
    if not plan.rows:
        detail = "; ".join(plan.errors[:5]) or "nothing to add"
        say(ctx, actor, f"No rows will be added. {detail}")
        return
    ask_confirm(
        actor, ctx, "import",
        {"rows": plan.rows},
        (
            f"Import will add {len(plan.rows)} customer(s). "
            f"Skipped duplicates: {plan.duplicates}. "
            f"Invalid rows: {sum(1 for item in plan.errors if 'duplicate' not in item)}.\n"
            "Existing rows are not changed or deleted. Confirm to append."
        ),
    )


def execute_confirmation(actor, ctx, token):
    found = ctx.repos.workflows.consume(token, actor["user_id"], ctx.now)
    if not found:
        ctx.repos.commit()
        say(ctx, actor, "That confirmation expired or was already used.")
        return
    kind = found["kind"]
    payload = found["payload"]
    if kind == "add":
        row = ctx.repos.customers.insert(payload["fields"])
        ctx.repos.commit()
        say(ctx, actor, f"Added #{row['id']} {clip(row['name'])}.")
    elif kind == "edit":
        row = ctx.repos.customers.update_partial(payload["id"], payload["changes"])
        ctx.repos.commit()
        if not row:
            say(ctx, actor, "That customer was not updated.")
        else:
            say(ctx, actor, f"Updated #{row['id']}.")
    elif kind == "delete":
        deleted = ctx.repos.customers.delete(payload["id"])
        ctx.repos.commit()
        say(ctx, actor, f"Deleted #{payload['id']}." if deleted else f"#{payload['id']} was already gone.")
    elif kind == "renew":
        row = ctx.repos.customers.get(payload["id"])
        if not row or row["start_date"] != payload["expected_start"]:
            ctx.repos.commit()
            say(ctx, actor, "That record changed. Open it again before renewing.")
            return
        new_start = add_one_month(date.fromisoformat(row["start_date"]))
        updated = ctx.repos.customers.set_cycle_if_start(
            payload["id"], payload["expected_start"], new_start.isoformat(), "Paid",
        )
        ctx.repos.commit()
        if not updated:
            say(ctx, actor, "That record changed. Open it again before renewing.")
            return
        view = compute_customer(updated, today=ctx.today)
        say(ctx, actor, f"Renewed #{updated['id']}. New expiry {view['expiry_date']}.")
    elif kind == "pay":
        row = ctx.repos.customers.update_partial(payload["id"], {"payment_status": payload["payment_status"]})
        ctx.repos.commit()
        if not row:
            say(ctx, actor, "That customer was not updated.")
        else:
            say(ctx, actor, f"Payment for #{row['id']} is now {row['payment_status']}. The renewal date was not changed.")
    elif kind == "import":
        added = 0
        existing = ctx.repos.customers.list_all()
        seen = {
            identity_key(item["phone"], item["account"], item["profile_name"], item["start_date"], ctx.country_code)
            for item in existing
        }
        for fields in payload["rows"]:
            key = identity_key(fields["phone"], fields["account"], fields["profile_name"], fields["start_date"], ctx.country_code)
            if key in seen:
                continue
            ctx.repos.customers.insert(fields)
            seen.add(key)
            added += 1
        ctx.repos.commit()
        say(ctx, actor, f"Imported {added} customer(s). Existing rows were not overwritten.")
    else:
        ctx.repos.commit()
        say(ctx, actor, "That confirmation is not valid.")


def send_export(actor, ctx):
    content = export_csv(ctx.repos.customers.list_all())
    ctx.outbox.send_document(actor["chat_id"], "customers.csv", content.encode("utf-8"))
    say(ctx, actor, "Export sent to this chat only.")


def ask_confirm(actor, ctx, kind, payload, text):
    token = new_token()
    ctx.repos.workflows.create_confirmation(
        actor["user_id"], kind, payload, ctx.now + ctx.settings.confirm_ttl, token,
    )
    ctx.repos.commit()
    say(ctx, actor, text, [[
        {"text": "Confirm", "callback_data": f"y:{token}"},
        {"text": "Cancel", "callback_data": f"n:{token}"},
    ]])


def filtered_views(ctx, filt):
    views = []
    for row in ctx.repos.customers.list_all():
        try:
            view = compute_customer(row, today=ctx.today)
        except (TypeError, ValueError, KeyError):
            continue
        if filt == "active" and view["status"] != "Active":
            continue
        if filt == "expiring" and view["status"] not in {"Expiring soon", "Expires today"}:
            continue
        if filt == "expired" and view["status"] != "Expired":
            continue
        views.append(view)
    views.sort(key=lambda view: (view["days_left"], view["id"]))
    return views


def validate_field(field, text, country_code):
    cleaned = text.strip()
    if field in {"name", "account", "profile"}:
        if not cleaned or len(cleaned) > 200:
            return None, "Send a value up to 200 characters."
        return cleaned, None
    if field == "phone":
        digits = normalize_phone(cleaned, country_code or "")
        if not digits:
            return None, "That phone number is not valid. Use + and a country code, or a national number."
        return digits, None
    if field == "amount":
        try:
            amount = float(cleaned)
        except ValueError:
            return None, "Send a number for the amount."
        if amount < 0 or amount != amount or amount > 1_000_000:
            return None, "Send an amount from 0 to 1000000."
        return amount, None
    if field == "start":
        try:
            date.fromisoformat(cleaned)
        except ValueError:
            return None, "Send the start date as YYYY-MM-DD."
        return cleaned, None
    if field == "pay":
        status = PAY_WORDS.get(cleaned.lower())
        if not status:
            return None, "Send paid or pending."
        return status, None
    return None, "That field cannot be edited."


def stored_fields(fields):
    return {
        "name": fields["name"],
        "phone": fields["phone"],
        "account": fields["account"],
        "profile_name": fields["profile"],
        "monthly_amount": fields["amount"],
        "start_date": fields["start"],
        "payment_status": fields["pay"],
    }


def prompt_for(step):
    prompts = {
        "phone": "Send the phone number, with +country code or a national number.",
        "account": "Send the Netflix account label.",
        "profile": "Send the profile name.",
        "amount": "Send the monthly amount.",
        "start": "Send the start date as YYYY-MM-DD.",
        "pay": "Send paid or pending.",
    }
    return prompts[step]


def summary_lines(view):
    return "\n".join([
        f"Name: {clip(view['name'])}",
        f"Phone: {view['phone']}",
        f"Account: {clip(view['account'])}",
        f"Profile: {clip(view['profile_name'])}",
        f"Amount: {format_amount(view['monthly_amount'])}",
        f"Start: {view['start_date']}",
        f"Expiry: {view['expiry_date']}",
        f"Payment: {view['payment_status']}",
    ])


def say(ctx, actor, text, buttons=None):
    ctx.outbox.send_message(actor["chat_id"], text[:3900], buttons)


def clip(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text[:200]


def parse_id(text):
    if re.fullmatch(r"\d{1,9}", text or ""):
        return int(text)
    return None


HELP = "\n".join([
    "Owner commands:",
    "/list [active|expiring|expired]",
    "/find text",
    "/show ID",
    "/add",
    "/edit ID",
    "/renew ID",
    "/pay ID paid|pending",
    "/delete ID",
    "/export",
    "/import",
    "/cancel",
    "Renew keeps the old cycle. Payment status does not move the date.",
    "Destructive actions ask you to confirm.",
])
