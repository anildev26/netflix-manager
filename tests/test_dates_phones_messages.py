import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from netflix_manager.dates import add_one_month, compute_customer, format_amount, today_ist
from netflix_manager.messages import (
    DEFAULT_TEMPLATE,
    renewal_link,
    renewal_message,
    render_template,
)
from netflix_manager.phones import normalize_phone

ROOT = Path(__file__).resolve().parent.parent


def test_month_clamp_and_leap():
    assert add_one_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert add_one_month(date(2026, 1, 30)) == date(2026, 2, 28)
    assert add_one_month(date(2024, 1, 31)) == date(2024, 2, 29)
    assert add_one_month(date(2026, 3, 31)) == date(2026, 4, 30)
    assert add_one_month(date(2026, 5, 31)) == date(2026, 6, 30)
    assert add_one_month(date(2026, 12, 15)) == date(2027, 1, 15)


def test_status_boundaries():
    today = date(2026, 5, 10)
    def view(days):
        start = today.replace(year=today.year) 
        expiry = today
        # Build a start whose expiry is today + days by walking a known month pair.
        target = date.fromordinal(today.toordinal() + days)
        # Use a start one month before the target when the day exists.
        row = {"id": 1, "name": "A", "phone": "1", "account": "a", "profile_name": "p",
               "monthly_amount": 1, "start_date": "2026-04-10", "payment_status": "Paid"}
        # Force expiry by choosing start = target shifted back via add_one_month inverse for non-clamped days.
        row["start_date"] = date(target.year if target.month > 1 else target.year - 1,
                                 target.month - 1 if target.month > 1 else 12,
                                 target.day).isoformat()
        computed = compute_customer(row, today=today)
        return computed
    # 10 May minus one month is 10 Apr, expiry 10 May, days 0.
    assert view(0)["status"] == "Expires today"
    row = {"start_date": "2026-04-08", "name": "A", "phone": "1", "account": "a",
           "profile_name": "p", "monthly_amount": 1, "payment_status": "Paid", "id": 1}
    assert compute_customer(row, today=date(2026, 5, 10))["status"] == "Expired"
    row["start_date"] = "2026-04-11"
    assert compute_customer(row, today=date(2026, 5, 10))["status"] == "Expiring soon"
    row["start_date"] = "2026-04-12"
    assert compute_customer(row, today=date(2026, 5, 10))["status"] == "Expiring soon"
    row["start_date"] = "2026-04-13"
    assert compute_customer(row, today=date(2026, 5, 10))["status"] == "Active"


def test_ist_midnight_boundary():
    before = datetime(2026, 3, 27, 18, 29, tzinfo=timezone.utc)
    after = datetime(2026, 3, 27, 18, 30, tzinfo=timezone.utc)
    assert today_ist(before) == date(2026, 3, 27)
    assert today_ist(after) == date(2026, 3, 28)
    row = {"id": 1, "name": "A", "phone": "919876543210", "account": "Account",
           "profile_name": "Profile", "monthly_amount": 10, "start_date": "2026-02-28",
           "payment_status": "Paid"}
    assert compute_customer(row, today=today_ist(before))["days_left"] == 1
    assert compute_customer(row, today=today_ist(after))["days_left"] == 0


def test_phone_country_codes():
    assert normalize_phone("9876543210") == "919876543210"
    assert normalize_phone("919876543210") == "919876543210"
    assert normalize_phone("+44 7911 123456") == "447911123456"
    assert normalize_phone("00447911123456") == "447911123456"
    assert normalize_phone("09876543210") == "919876543210"
    assert normalize_phone("+1 415 555 2671") == "14155552671"
    assert normalize_phone("12345") is None
    assert normalize_phone("call me") is None
    assert normalize_phone("") is None
    assert normalize_phone("9876543210", "1") == "19876543210"
    assert normalize_phone("+44 7911 123456", "1") == "447911123456"


def test_template_does_not_evaluate_attributes_or_keep_newlines():
    rendered = render_template("Hello {name.__class__} {name}", {"name": "Ada\n<script>"})
    assert "class" not in rendered
    assert "<script>" not in rendered
    assert "Ada" in rendered
    assert "\n" not in rendered.split("Hello ", 1)[1]
    message = renewal_message({
        "name": "Ada",
        "profile_name": "Profile",
        "account": "Account 1",
        "monthly_amount": 199,
        "expiry_date": "2026-04-16",
        "days_left": 2,
    }, DEFAULT_TEMPLATE)
    assert "expires on *16 Apr 2026* (2 days remaining)" in message
    assert "₹199" in message
    _text, url = renewal_link({
        "name": "Ada",
        "profile_name": "Profile",
        "account": "Account 1",
        "monthly_amount": 1234567,
        "phone": "+44 7911 123456",
        "expiry_date": "2026-04-16",
        "days_left": 0,
    }, "91")
    assert url.startswith("https://wa.me/447911123456?text=")
    assert format_amount(1234567) == "12,34,567"
    assert format_amount(199) == "199"


def test_javascript_month_clamp_matches_python():
    script = r"""
const { addOneMonthISO } = require("./public/dates.js");
const cases = ["2026-01-31","2026-01-30","2024-01-31","2026-03-31","2026-05-31","2026-02-28","2026-12-15"];
const out = {};
for (const iso of cases) out[iso] = addOneMonthISO(iso);
out.chain = addOneMonthISO(addOneMonthISO("2026-01-31"));
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
    got = json.loads(result.stdout)
    for iso, expected in got.items():
        if iso == "chain":
            assert expected == "2026-03-28"
            continue
        assert expected == add_one_month(date.fromisoformat(iso)).isoformat()
    assert "node" 
    assert sys.version
