"""Calendar-month expiry. Renewal adds one clamped month and does not use today."""

from __future__ import annotations

import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def add_one_month(day: date) -> date:
    """Add one calendar month, clamping to the last real day of the target month."""
    month = day.month + 1
    year = day.year
    if month > 12:
        month = 1
        year += 1
    clamped = min(day.day, calendar.monthrange(year, month)[1])
    return date(year, month, clamped)


def today_ist(now: datetime | None = None) -> date:
    """Calendar date in Asia/Kolkata. `now` must be timezone-aware when supplied."""
    if now is None:
        current = datetime.now(IST)
    else:
        if now.tzinfo is None:
            raise ValueError("timezone-aware datetime required")
        current = now.astimezone(IST)
    return current.date()


def compute_customer(row: dict, today: date | None = None) -> dict:
    """Attach expiry, days_left, and status. Does not write to the database."""
    view = dict(row)
    start = date.fromisoformat(str(view["start_date"]))
    expiry = add_one_month(start)
    current = today if today is not None else date.today()
    days = (expiry - current).days
    view["expiry_date"] = expiry.isoformat()
    view["days_left"] = days
    if days < 0:
        view["status"] = "Expired"
    elif days == 0:
        view["status"] = "Expires today"
    elif days <= 2:
        view["status"] = "Expiring soon"
    else:
        view["status"] = "Active"
    return view


def format_display_date(iso: str) -> str:
    day = date.fromisoformat(iso)
    return f"{day.day:02d} {MONTHS[day.month - 1]} {day.year}"


def format_amount(value) -> str:
    number = float(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number.is_integer():
        whole = str(int(number))
        fraction = ""
    else:
        text = f"{number:.2f}".rstrip("0")
        whole, fraction = text.split(".")
        fraction = "." + fraction
    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.append(head[-2:])
            head = head[:-2]
        if head:
            parts.append(head)
        grouped = ",".join(reversed(parts)) + "," + tail
    return sign + grouped + fraction


