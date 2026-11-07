import json
from datetime import datetime, timedelta, timezone
from urllib import error

import pytest

import server
from netflix_manager.bot import BotContext, handle_update
from netflix_manager.config import BotSettings
from netflix_manager.dates import today_ist
from netflix_manager.repos import MemoryRepos
from netflix_manager.telegram_api import TelegramError, fetch_file, send_message
from tests.conftest import auth, login

TOKEN = "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"
SECRET = "webhook-secret-value"
OWNERS = frozenset({42})


class Box:
    def __init__(self):
        self.messages = []
        self.docs = []
        self.answers = []

    def send_message(self, chat_id, text, buttons=None):
        self.messages.append({"chat_id": chat_id, "text": text, "buttons": buttons})

    def send_document(self, chat_id, filename, data):
        self.docs.append({"chat_id": chat_id, "filename": filename, "data": data})

    def answer(self, callback_id, text):
        self.answers.append((callback_id, text))


def settings():
    return BotSettings(True, TOKEN, SECRET, OWNERS, timedelta(minutes=10))


def ctx(repos, box, now=None, fetcher=None):
    return BotContext(
        settings=settings(),
        repos=repos,
        outbox=box,
        now=now or datetime(2026, 3, 20, 3, 30, tzinfo=timezone.utc),
        country_code="91",
        fetcher=fetcher or (lambda _file_id: b""),
    )


def message(update_id, text, user=42):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "from": {"id": user},
            "chat": {"id": user, "type": "private"},
        },
    }


def callback(update_id, data, user=42):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": user},
            "message": {"chat": {"id": user, "type": "private"}},
        },
    }


def token_from(box):
    buttons = box.messages[-1]["buttons"]
    return buttons[0][0]["callback_data"].split(":", 1)[1]


def test_webhook_auth(client, monkeypatch):
    assert client.post("/api/telegram/webhook", json={"update_id": 1}).status_code == 404
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_OWNER_IDS", "42")
    sent = []
    monkeypatch.setattr(server, "send_message", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr("netflix_manager.telegram_api.answer_callback", lambda *args, **kwargs: None)
    bad = client.post("/api/telegram/webhook", json=message(1, "/help"), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert bad.status_code == 401
    assert sent == []
    ok = client.post(
        "/api/telegram/webhook",
        json=message(1, "/help"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert ok.status_code == 200
    assert sent and sent[0][1] == 42
    assert "secret" not in sent[0][2].lower()


def test_ignores_non_owner_group_and_replay():
    repos = MemoryRepos()
    box = Box()
    context = ctx(repos, box)
    assert handle_update(message(1, "/help", user=7), context) == "ignored"
    assert handle_update({
        "update_id": 2,
        "message": {"text": "/help", "from": {"id": 42}, "chat": {"id": -100, "type": "group"}},
    }, context) == "ignored"
    assert box.messages == []
    assert handle_update(message(3, "/help"), context) == "ok"
    assert handle_update(message(3, "/help"), context) == "duplicate"
    assert len(box.messages) == 1
    assert box.messages[0]["chat_id"] == 42


def test_add_edit_pay_delete_and_confirmations():
    repos = MemoryRepos()
    box = Box()
    now = datetime(2026, 3, 20, 3, 30, tzinfo=timezone.utc)
    context = ctx(repos, box, now)
    steps = ["/add", "Ada", "9876543210", "Account 1", "Ada Profile", "199", "2026-01-31", "paid"]
    for index, text in enumerate(steps, start=1):
        assert handle_update(message(index, text), context) == "ok"
    confirm = token_from(box)
    assert "2026-02-28" in box.messages[-1]["text"]
    assert handle_update(callback(20, f"y:{confirm}"), context) == "ok"
    row = repos.customers.list_all()[0]
    assert row["phone"] == "919876543210"
    assert row["start_date"] == "2026-01-31"
    assert handle_update(callback(21, f"y:{confirm}"), context) == "ok"
    assert "already used" in box.messages[-1]["text"]
    assert len(repos.customers.list_all()) == 1

    assert handle_update(message(22, f"/pay {row['id']} pending"), context) == "ok"
    assert "will not change" in box.messages[-1]["text"]
    pay = token_from(box)
    assert handle_update(callback(23, f"y:{pay}"), context) == "ok"
    assert repos.customers.get(row["id"])["start_date"] == "2026-01-31"
    assert repos.customers.get(row["id"])["payment_status"] == "Payment pending"

    assert handle_update(message(24, f"/renew {row['id']}"), context) == "ok"
    assert "2026-02-28" in box.messages[-1]["text"]
    assert "2026-03-28" in box.messages[-1]["text"]
    assert "does not start from today" in box.messages[-1]["text"]
    renew = token_from(box)
    assert handle_update(callback(25, f"n:{renew}"), context) == "ok"
    assert repos.customers.get(row["id"])["start_date"] == "2026-01-31"
    assert handle_update(message(26, f"/renew {row['id']}"), context) == "ok"
    assert handle_update(callback(27, f"y:{token_from(box)}"), context) == "ok"
    assert repos.customers.get(row["id"])["start_date"] == "2026-02-28"

    maker = ctx(repos, box, now + timedelta(minutes=12))
    assert handle_update(message(28, f"/delete {row['id']}"), maker) == "ok"
    stale = token_from(box)
    later = ctx(repos, box, now + timedelta(minutes=23))
    assert handle_update(callback(29, f"y:{stale}"), later) == "ok"
    assert "expired" in box.messages[-1]["text"]
    assert repos.customers.get(row["id"]) is not None
    fresh = ctx(repos, box, now + timedelta(minutes=12))
    assert handle_update(message(30, f"/delete {row['id']}"), fresh) == "ok"
    assert handle_update(callback(31, f"y:{token_from(box)}"), fresh) == "ok"
    assert repos.customers.get(row["id"]) is None


def test_other_owner_cannot_consume_token():
    repos = MemoryRepos()
    box = Box()
    both = BotSettings(True, TOKEN, SECRET, frozenset({42, 43}), timedelta(minutes=10))
    context = ctx(repos, box)
    context.settings = both
    repos.customers.insert({
        "name": "Ada", "phone": "919876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 10, "start_date": "2026-01-15",
        "payment_status": "Paid",
    })
    assert handle_update(message(1, "/delete 1"), context) == "ok"
    stolen = callback(2, f"y:{token_from(box)}", user=43)
    assert handle_update(stolen, context) == "ok"
    assert repos.customers.get(1) is not None
    assert "expired or was already used" in box.messages[-1]["text"]


def test_list_filter_search_and_export_import():
    repos = MemoryRepos()
    box = Box()
    now = datetime(2026, 3, 26, 3, 30, tzinfo=timezone.utc)
    context = ctx(repos, box, now, fetcher=lambda _file_id: (
        "Name,Phone,Account,Profile Name,Monthly Amount,Start Date,Payment Status\n"
        "=cmd,919000000099,Other,Profile,10,2026-02-26,Paid\n"
        "Ada,919876543210,Account,Profile,10,2026-02-28,Paid\n"
    ).encode())
    repos.customers.insert({
        "name": "Ada", "phone": "919876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 10, "start_date": "2026-02-28",
        "payment_status": "Paid",
    })
    assert today_ist(now).isoformat() == "2026-03-26"
    assert handle_update(message(1, "/list expiring"), context) == "ok"
    assert "Ada" in box.messages[-1]["text"]
    assert handle_update(message(2, "/find Account"), context) == "ok"
    assert handle_update(message(3, "/export"), context) == "ok"
    assert box.docs[-1]["chat_id"] == 42
    assert b"'=cmd" not in box.docs[-1]["data"]
    assert handle_update(message(4, "/import"), context) == "ok"
    imported = {
        "update_id": 5,
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "document": {"file_id": "file1", "file_name": "customers.csv", "file_size": 100},
        },
    }
    assert handle_update(imported, context) == "ok"
    assert "add 1" in box.messages[-1]["text"]
    assert "919000000099" not in box.messages[-1]["text"]
    assert handle_update(callback(6, f"y:{token_from(box)}"), context) == "ok"
    names = [row["name"] for row in repos.customers.list_all()]
    assert names.count("Ada") == 1
    assert "=cmd" in names
    assert handle_update(message(7, "/export"), context) == "ok"
    assert b"'=cmd" in box.docs[-1]["data"]


def test_leap_renewal_text():
    repos = MemoryRepos()
    box = Box()
    context = ctx(repos, box, datetime(2024, 3, 1, 4, 0, tzinfo=timezone.utc))
    repos.customers.insert({
        "name": "Leap", "phone": "919876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 10, "start_date": "2024-01-31",
        "payment_status": "Payment pending",
    })
    assert handle_update(message(1, "/renew 1"), context) == "ok"
    assert "2024-02-29" in box.messages[-1]["text"]


def test_outbox_refuses_other_destinations():
    outbox = server._BotOutbox(settings())
    with pytest.raises(TelegramError):
        outbox.send_message(999, "hello")


def test_telegram_errors_hide_token():
    def opener(req, timeout=0):
        raise error.HTTPError(req.full_url, 403, "no", hdrs=None, fp=None)

    with pytest.raises(TelegramError) as caught:
        send_message(TOKEN, 42, "hello", opener=opener)
    assert TOKEN not in str(caught.value)
    assert "api.telegram.org" not in str(caught.value)

    def bad_path(req, timeout=0):
        class Resp:
            status = 200
            def read(self):
                if req.full_url.endswith("/getFile"):
                    return json.dumps({"ok": True, "result": {"file_path": "https://evil.example/x"}}).encode()
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        return Resp()

    with pytest.raises(TelegramError) as caught:
        fetch_file(TOKEN, "abc", opener=bad_path)
    assert "evil.example" not in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_reminders_milestones_dedupe_and_failure(client, memory, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("REMINDERS_DRY_RUN", "false")
    monkeypatch.setattr(server, "today_ist", lambda: __import__("datetime").date(2026, 3, 26))
    memory.customers.insert({
        "name": "Soon", "phone": "9876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 199, "start_date": "2026-02-28",
        "payment_status": "Paid",
    })
    memory.customers.insert({
        "name": "Today", "phone": "12345", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 50, "start_date": "2026-02-26",
        "payment_status": "Payment pending",
    })
    memory.customers.insert({
        "name": "Overdue", "phone": "919111111111", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 50, "start_date": "2026-01-01",
        "payment_status": "Paid",
    })
    sent = []
    failures = {"count": 0}

    def fake_send(token, chat_id, text, buttons=None, opener=None):
        assert chat_id == 42
        assert token == TOKEN
        if "Today" in text and failures["count"] == 0:
            failures["count"] += 1
            raise TelegramError("telegram status 500")
        sent.append(text)

    monkeypatch.setattr(server, "send_message", fake_send)
    headers = {"Authorization": "Bearer cron-secret-value1"}
    first = client.post("/api/cron/reminders", headers=headers, json={"chat_id": 999})
    body = first.get_json()
    assert first.status_code == 200
    assert body["sent"] == 1
    assert body["failed"] == 1
    assert body["matched"] == 2
    assert "999" not in first.get_data(as_text=True)
    assert "9876543210" not in first.get_data(as_text=True)
    assert any("https://wa.me/919876543210?text=" in text for text in sent)
    assert any("Soon" in text for text in sent)
    assert all("Overdue" not in text for text in sent)
    second = client.post("/api/cron/reminders", headers=headers)
    assert second.get_json()["sent"] == 1
    assert second.get_json()["skipped"] == 1
    assert any("No valid WhatsApp number" in text for text in sent)


def test_reminders_disabled_and_unauthorized(client, memory, monkeypatch):
    assert client.get("/api/cron/reminders").status_code == 401
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value1")
    assert client.get("/api/cron/reminders?secret=cron-secret-value1").status_code == 401
    assert memory.customers.calls == 0
    ok = client.get("/api/cron/reminders", headers={"Authorization": "Bearer cron-secret-value1"})
    assert ok.get_json()["enabled"] is False
    assert memory.customers.calls == 0
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    bad = client.get("/api/cron/reminders", headers={"Authorization": "Bearer cron-secret-value1"})
    assert bad.status_code == 500
    assert "-100999" not in bad.get_data(as_text=True)
    assert memory.customers.calls == 0


def test_dry_run_does_not_send(client, memory, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(server, "today_ist", lambda: __import__("datetime").date(2026, 3, 26))
    memory.customers.insert({
        "name": "Soon", "phone": "919876543210", "account": "Account",
        "profile_name": "Profile", "monthly_amount": 10, "start_date": "2026-02-28",
        "payment_status": "Paid",
    })
    called = []
    monkeypatch.setattr(server, "send_message", lambda *args, **kwargs: called.append(args))
    response = client.get("/api/cron/reminders", headers={"Authorization": "Bearer cron-secret-value1"})
    body = response.get_json()
    assert body["dry_run"] is True
    assert body["matched"] == 1
    assert called == []
    assert memory.deliveries.rows == {}
