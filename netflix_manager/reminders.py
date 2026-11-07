"""Owner expiry reminders. Sends nothing by itself; the caller supplies `send`."""

from __future__ import annotations

from netflix_manager.dates import compute_customer, format_amount
from netflix_manager.messages import renewal_link
from netflix_manager.repos import STALE_MINUTES
from netflix_manager.telegram_api import TelegramError


def milestone_name(day: int) -> str:
    return f"days:{int(day)}"


def reminder_heading(day: int) -> str:
    if day == 0:
        return "Expires today"
    return f"Expires in {day} days"


def build_reminder_text(view: dict, day: int, template: str, country_code: str | None) -> str:
    message, url = renewal_link(view, country_code, template)
    lines = [
        reminder_heading(day),
        f"#{view['id']} {view['name']}",
        f"Profile: {view['profile_name']}",
        f"Account: {view['account']}",
        f"Expiry: {view['expiry_date']}",
        f"Amount: {format_amount(view['monthly_amount'])}",
        "",
        "WhatsApp draft:",
        message,
    ]
    if url:
        lines.extend(["", url])
    else:
        lines.extend(["", "No valid WhatsApp number is stored, so no link was created."])
    return "\n".join(lines)[:4000]


def run_reminders(*, rows, today, milestones, template, country_code, dry_run, store, send, now, limit=30, commit=None):
    commit = commit or (lambda: None)
    due = []
    invalid = 0
    for row in rows:
        try:
            view = compute_customer(row, today=today)
        except (TypeError, ValueError, KeyError):
            invalid += 1
            continue
        for day in milestones:
            if view["days_left"] == day:
                due.append((view, day))
    due.sort(key=lambda item: (item[0]["days_left"], item[0]["id"]))
    summary = {
        "ok": True,
        "enabled": True,
        "dry_run": bool(dry_run),
        "matched": len(due),
        "invalid": invalid,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "deferred": 0,
    }
    if dry_run:
        return summary
    progressed = 0
    for view, day in due:
        if progressed >= limit:
            summary["deferred"] += 1
            continue
        milestone = milestone_name(day)
        state = store.claim(view["id"], view["expiry_date"], milestone, now)
        if state != "claimed":
            summary["skipped"] += 1
            continue
        progressed += 1
        commit()
        try:
            send(build_reminder_text(view, day, template, country_code))
        except TelegramError as exc:
            store.mark_failed(view["id"], view["expiry_date"], milestone, str(exc)[:200], now)
            commit()
            summary["failed"] += 1
            continue
        except Exception:
            store.mark_failed(view["id"], view["expiry_date"], milestone, "send failed", now)
            commit()
            summary["failed"] += 1
            continue
        store.mark_sent(view["id"], view["expiry_date"], milestone, now)
        commit()
        summary["sent"] += 1
    summary["stale_minutes"] = STALE_MINUTES
    return summary
