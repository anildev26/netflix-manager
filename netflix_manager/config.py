"""Environment configuration.

Secrets are read on each call and never given fallback values. Missing or
malformed secrets disable the feature that needs them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta

import hmac


def const_eq(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _raw(name: str) -> str:
    return os.environ.get(name, "")


def auth_problems() -> list[str]:
    """Names of auth settings that are missing or too weak. Values are not returned."""
    problems = []
    secret = _raw("JWT_SECRET")
    user = _raw("ADMIN_USERNAME")
    password = _raw("ADMIN_PASSWORD")
    if secret != secret.strip() or len(secret) < 32 or any(ch.isspace() for ch in secret):
        problems.append("JWT_SECRET")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
        problems.append("ADMIN_USERNAME")
    if password != password.strip() or len(password) < 12 or "\n" in password or "\r" in password:
        problems.append("ADMIN_PASSWORD")
    return problems


def auth_material() -> tuple[str, str, str] | None:
    if auth_problems():
        return None
    return _raw("ADMIN_USERNAME"), _raw("ADMIN_PASSWORD"), _raw("JWT_SECRET")


def demo_credentials() -> tuple[str, str] | None:
    """Demo login is off unless both values are explicitly strong.

    The demo role never receives real customer rows. A partial or weak pair
    disables demo instead of failing the admin site.
    """
    user = _raw("TEST_USERNAME")
    password = _raw("TEST_PASSWORD")
    admin = _raw("ADMIN_USERNAME")
    if not user or not password:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
        return None
    if user == admin:
        return None
    if password != password.strip() or len(password) < 12 or "\n" in password or "\r" in password:
        return None
    return user, password


def setup_token() -> str | None:
    raw = _raw("SETUP_TOKEN")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", raw):
        return raw
    return None


def cron_secret() -> str | None:
    raw = _raw("CRON_SECRET")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", raw):
        return raw
    return None


def cron_authorized(header: str) -> bool:
    secret = cron_secret()
    if not secret or not header or not header.startswith("Bearer "):
        return False
    presented = header[len("Bearer "):]
    if not presented or presented != presented.strip():
        return False
    return const_eq(presented, secret)


def telegram_token() -> str | None:
    raw = _raw("TELEGRAM_BOT_TOKEN")
    if re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", raw):
        return raw
    return None


def telegram_chat_id() -> int | None:
    raw = _raw("TELEGRAM_CHAT_ID").strip()
    if re.fullmatch(r"[1-9]\d{0,15}", raw):
        return int(raw)
    return None


def owner_ids():
    """None when unset, 'invalid' when set badly, otherwise a frozenset of ids."""
    raw = _raw("TELEGRAM_OWNER_IDS").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts or len(parts) > 5:
        return "invalid"
    found = []
    for part in parts:
        if not re.fullmatch(r"[1-9]\d{0,15}", part):
            return "invalid"
        found.append(int(part))
    return frozenset(found)


def webhook_secret() -> str | None:
    raw = _raw("TELEGRAM_WEBHOOK_SECRET")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,256}", raw):
        return raw
    return None


def confirm_ttl() -> timedelta:
    raw = _raw("TELEGRAM_CONFIRM_TTL_SECONDS").strip()
    if not raw:
        return timedelta(minutes=10)
    if not re.fullmatch(r"\d{2,4}", raw):
        return timedelta(minutes=10)
    seconds = int(raw)
    if seconds < 30 or seconds > 3600:
        return timedelta(minutes=10)
    return timedelta(seconds=seconds)


def default_country_code() -> str | None:
    raw = _raw("DEFAULT_COUNTRY_CODE").strip()
    if raw == "":
        return "91"
    if re.fullmatch(r"[1-9]\d{0,2}", raw):
        return raw
    return None


def dry_run_enabled() -> bool:
    raw = _raw("REMINDERS_DRY_RUN").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def milestone_days() -> tuple[int, ...] | None:
    raw = _raw("REMINDER_MILESTONE_DAYS").strip()
    if raw == "":
        return (2, 0)
    return _parse_milestones(raw)


def _parse_milestones(raw: str) -> tuple[int, ...] | None:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts or len(parts) > 5:
        return None
    values: list[int] = []
    for part in parts:
        if not re.fullmatch(r"\d{1,2}", part):
            return None
        day = int(part)
        if day > 14:
            return None
        if day not in values:
            values.append(day)
    if not values:
        return None
    return tuple(values)


@dataclass(frozen=True)
class BotSettings:
    enabled: bool
    token: str | None
    webhook_secret: str | None
    owner_ids: frozenset
    confirm_ttl: timedelta


def load_bot_settings() -> BotSettings:
    token = telegram_token()
    secret = webhook_secret()
    owners = owner_ids()
    enabled = bool(token and secret and isinstance(owners, frozenset) and owners)
    return BotSettings(
        enabled=enabled,
        token=token if enabled else None,
        webhook_secret=secret if enabled else None,
        owner_ids=owners if isinstance(owners, frozenset) else frozenset(),
        confirm_ttl=confirm_ttl(),
    )


def reminder_gate() -> str:
    """disabled | bad_destination | ready."""
    token = telegram_token()
    raw_chat = _raw("TELEGRAM_CHAT_ID").strip()
    owners = owner_ids()
    if owners == "invalid":
        return "bad_destination"
    if not token or not raw_chat:
        return "disabled"
    chat_id = telegram_chat_id()
    if chat_id is None:
        return "bad_destination"
    if isinstance(owners, frozenset) and chat_id not in owners:
        return "bad_destination"
    return "ready"
