"""Website, bot, and reminders must share the Asia/Kolkata business date."""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from netflix_manager.bot import BotContext, handle_update
from netflix_manager.dates import IST, today_ist
from tests.conftest import auth, login
from tests.test_bot_and_reminders import TOKEN, Box, message, settings

ROOT = Path(__file__).resolve().parent.parent


def freeze(monkeypatch, moment):
    real_datetime = datetime

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return moment
            return moment.astimezone(tz)

    monkeypatch.setattr("netflix_manager.dates.datetime", Frozen)


def api_views(client, token):
    response = client.get("/api/customers", headers=auth(token))
    assert response.status_code == 200
    return {row["name"]: row for row in response.get_json()}


def bot_card(repos, moment, cid, update_id):
    box = Box()
    context = BotContext(settings(), repos, box, moment, "91", lambda _file_id: b"")
    assert handle_update(message(update_id, f"/show {cid}"), context) == "ok"
    return box.messages[-1]["text"]


def test_statuses_match_across_ist_midnight(client, memory, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret-value1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    token = login(client)
    headers = {"Authorization": "Bearer cron-secret-value1"}
    early = {
        "name": "Early",
        "phone": "919876543210",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-02-26",
        "payment_status": "Paid",
    }
    later = dict(early, name="Later", start_date="2026-02-28")
    assert client.post("/api/customers", headers=auth(token), json=early).status_code == 201
    assert client.post("/api/customers", headers=auth(token), json=later).status_code == 201

    checkpoints = [
        # UTC instant, IST date, Early days/status, Later days/status, reminder matches
        (datetime(2026, 3, 23, 18, 29, tzinfo=timezone.utc), "2026-03-23", 3, "Active", 5, "Active", 0),
        (datetime(2026, 3, 23, 18, 30, tzinfo=timezone.utc), "2026-03-24", 2, "Expiring soon", 4, "Active", 1),
        (datetime(2026, 3, 25, 18, 29, tzinfo=timezone.utc), "2026-03-25", 1, "Expiring soon", 3, "Active", 0),
        (datetime(2026, 3, 25, 18, 30, tzinfo=timezone.utc), "2026-03-26", 0, "Expires today", 2, "Expiring soon", 2),
        (datetime(2026, 3, 27, 18, 29, tzinfo=timezone.utc), "2026-03-27", -1, "Expired", 1, "Expiring soon", 0),
        (datetime(2026, 3, 27, 18, 30, tzinfo=timezone.utc), "2026-03-28", -2, "Expired", 0, "Expires today", 1),
    ]
    for index, (moment, ist_day, early_days, early_status, later_days, later_status, matched) in enumerate(checkpoints):
        freeze(monkeypatch, moment)
        assert today_ist() == datetime.fromisoformat(ist_day).date()
        assert today_ist().isoformat() != moment.date().isoformat() or ist_day == moment.date().isoformat()
        views = api_views(client, token)
        for name, days, status, expiry in (
            ("Early", early_days, early_status, "2026-03-26"),
            ("Later", later_days, later_status, "2026-03-28"),
        ):
            row = views[name]
            assert row["expiry_date"] == expiry
            assert row["days_left"] == days
            assert row["status"] == status
            card = bot_card(memory, moment, row["id"], 100 + index * 10 + days)
            assert f"Expiry: {expiry} ({days} days)" in card
            assert f"Status: {status}" in card
        cron = client.get("/api/cron/reminders", headers=headers)
        assert cron.status_code == 200
        assert cron.get_json()["matched"] == matched
        assert cron.get_json()["dry_run"] is True


def test_new_dates_use_ist_not_utc(client, monkeypatch):
    token = login(client)
    moment = datetime(2026, 3, 27, 18, 30, tzinfo=timezone.utc)
    freeze(monkeypatch, moment)
    created = client.post("/api/customers", headers=auth(token), json={
        "name": "New",
        "phone": "919876543210",
        "account": "Account",
        "profile_name": "Profile",
        "monthly_amount": 10,
        "start_date": "2026-03-01",
        "payment_status": "Paid",
    })
    assert created.status_code == 201
    assert created.get_json()["created_at"] == "2026-03-28"
    exported = client.get("/api/customers/export", headers=auth(token))
    assert "netflix_customers_2026-03-28.csv" in exported.headers["Content-Disposition"]
    link = client.get(f"/api/customers/{created.get_json()['id']}/whatsapp", headers=auth(token))
    assert "expires on *01 Apr 2026*" in link.get_json()["message"]


def test_browser_date_formatting_does_not_shift_the_day():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert "toISOString" not in html
    assert "toLocaleDateString" not in html
    assert "businessTodayISO" in html
    assert "formatCalendarDate" in html
    script = r"""
const api = require("./public/dates.js");
const before = new Date("2026-03-27T18:29:00Z");
const after = new Date("2026-03-27T18:30:00Z");
process.stdout.write(JSON.stringify({
  before: api.businessTodayISO(before),
  after: api.businessTodayISO(after),
  utcSlice: after.toISOString().slice(0, 10),
  shown: api.formatCalendarDate("2026-03-28"),
  chain: api.addOneMonthISO("2026-01-31")
}));
"""
    for zone in ("UTC", "Asia/Kolkata", "America/Los_Angeles", "Pacific/Honolulu"):
        env = os.environ.copy()
        env["TZ"] = zone
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True, env=env,
        )
        got = json.loads(result.stdout)
        assert got["before"] == "2026-03-27"
        assert got["after"] == "2026-03-28"
        assert got["utcSlice"] == "2026-03-27"
        assert got["shown"] == "28 Mar 2026"
        assert got["chain"] == "2026-02-28"
    assert today_ist(datetime(2026, 3, 27, 18, 30, tzinfo=timezone.utc)).isoformat() == "2026-03-28"
    assert IST.key == "Asia/Kolkata"
