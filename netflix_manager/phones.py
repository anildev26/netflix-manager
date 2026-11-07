"""Phone numbers for wa.me links. A link is only built for a valid E.164 digit string."""

from __future__ import annotations

import re


def normalize_phone(raw, default_country_code: str = "91") -> str | None:
    """Return digits for wa.me, or None when the number should not become a link.

    Numbers that start with + or 00 are international. Anything else is a
    national number and receives `default_country_code`, unless it already
    begins with that code and has a plausible length.
    """
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text or re.search(r"[A-Za-z]", text):
        return None
    if not re.fullmatch(r"[1-9]\d{0,2}", default_country_code or ""):
        return None
    explicit = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if text.startswith("00"):
        digits = digits[2:]
        if not digits:
            return None
    cc = default_country_code
    if explicit:
        if digits.startswith(cc) and len(digits) > len(cc) and digits[len(cc)] == "0":
            digits = cc + digits[len(cc) + 1:]
    else:
        if digits.startswith("0"):
            digits = digits[1:]
        already = (
            digits.startswith(cc)
            and len(cc) < len(digits) <= 15
            and len(digits) - len(cc) >= 8
        )
        if already:
            pass
        elif 8 <= len(digits) <= 12:
            digits = cc + digits
        else:
            return None
    if not re.fullmatch(r"[1-9]\d{7,14}", digits):
        return None
    return digits
