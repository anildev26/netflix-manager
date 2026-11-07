"""Renewal copy shared by the website button and Telegram reminders.

The default wording matches the existing website draft. Placeholders are
substituted literally. Attribute access such as {name.__class__} is not evaluated.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from netflix_manager.dates import format_amount, format_display_date
from netflix_manager.phones import normalize_phone

ALLOWED_TOKENS = {
    "name",
    "profile_name",
    "account",
    "expiry_phrase",
    "expiry_date",
    "days_left",
    "amount",
}
USER_TOKENS = {"name", "profile_name", "account"}

DEFAULT_TEMPLATE = (
    "Hi {name}! \U0001F44B\n\n"
    "Your Netflix subscription (Profile: *{profile_name}* | Account: {account}) {expiry_phrase}.\n\n"
    "\U0001F4B0 Monthly amount: *\u20b9{amount}*\n\n"
    "Please renew to continue uninterrupted Netflix access. \U0001F3AC\n\n"
    "Thank you!"
)

_TOKEN = re.compile(r"\{([^{}]+)\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InvalidTemplate(ValueError):
    pass


def message_template() -> str:
    raw = os.environ.get("REMINDER_MESSAGE_TEMPLATE")
    if raw is None or raw.strip() == "":
        return DEFAULT_TEMPLATE
    if len(raw) > 1500 or "\x00" in raw:
        raise InvalidTemplate()
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw


def sanitize_field(value) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _CONTROL.sub("", text)
    text = text.replace("*", "").replace("_", "").replace("`", "")
    text = text.replace("<", "").replace(">", "")
    return text.strip()


def render_template(template: str, fields: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in ALLOWED_TOKENS:
            return ""
        value = fields.get(key, "")
        if key in USER_TOKENS:
            return sanitize_field(value)
        return "" if value is None else str(value)

    return _TOKEN.sub(replace, template)


def expiry_phrase(days_left: int, expiry_iso: str) -> str:
    shown = format_display_date(expiry_iso)
    if days_left > 0:
        return f"expires on *{shown}* ({days_left} days remaining)"
    if days_left == 0:
        return f"expires *TODAY* ({shown})"
    return f"has already expired on *{shown}*"


def renewal_message(view: dict, template: str | None = None) -> str:
    chosen = template if template is not None else DEFAULT_TEMPLATE
    fields = {
        "name": view.get("name", ""),
        "profile_name": view.get("profile_name", ""),
        "account": view.get("account", ""),
        "expiry_date": format_display_date(view["expiry_date"]),
        "days_left": view.get("days_left", ""),
        "amount": format_amount(view.get("monthly_amount", 0)),
        "expiry_phrase": expiry_phrase(int(view["days_left"]), view["expiry_date"]),
    }
    return render_template(chosen, fields)


def encode_uri_component(value: str) -> str:
    """Match JavaScript encodeURIComponent for the wa.me text parameter."""
    return quote(value, safe="!*'()")


def whatsapp_url(phone_digits: str, message: str) -> str | None:
    if not re.fullmatch(r"[1-9]\d{7,14}", phone_digits or ""):
        return None
    if "\n" in phone_digits or " " in phone_digits:
        return None
    return f"https://wa.me/{phone_digits}?text={encode_uri_component(message)}"


def renewal_link(view: dict, country_code: str | None, template: str | None = None):
    """Return (message, url or None). Never contacts WhatsApp."""
    message = renewal_message(view, template)
    if not country_code:
        return message, None
    digits = normalize_phone(view.get("phone"), country_code)
    if not digits:
        return message, None
    return message, whatsapp_url(digits, message)
