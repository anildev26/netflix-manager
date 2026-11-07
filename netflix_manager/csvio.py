"""CSV export and import shared by the website and the owner bot.

Export neutralises spreadsheet formulas. Import only appends, skips duplicates,
and rejects oversized files. It does not delete or update existing rows.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

from netflix_manager.phones import normalize_phone

HEADERS = ["Name", "Phone", "Account", "Profile Name", "Monthly Amount", "Start Date", "Payment Status"]
MAX_ROWS = 200
MAX_BYTES = 200_000
MAX_FIELD = 200
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA):
        return "'" + text
    return text


def csv_unescape(value: str) -> str:
    if value.startswith("'") and len(value) > 1 and value[1] in "=+-@\t\r":
        return value[1:]
    return value


def export_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow([
            csv_safe(row["name"]),
            csv_safe(row["phone"]),
            csv_safe(row["account"]),
            csv_safe(row["profile_name"]),
            csv_safe(row["monthly_amount"]),
            csv_safe(row["start_date"]),
            csv_safe(row["payment_status"]),
        ])
    return buffer.getvalue()


def identity_key(phone, account, profile, start, country_code: str) -> tuple:
    digits = normalize_phone(phone, country_code)
    phone_key = digits if digits else re.sub(r"\D", "", str(phone or ""))
    return (
        phone_key,
        str(account or "").strip().casefold(),
        str(profile or "").strip().casefold(),
        str(start or "").strip(),
    )


@dataclass
class ImportPlan:
    rows: list[dict]
    errors: list[str]
    duplicates: int


def plan_import(content: str, existing: list[dict], country_code: str) -> ImportPlan:
    if len(content.encode("utf-8")) > MAX_BYTES:
        return ImportPlan([], ["File is too large"], 0)
    try:
        reader = csv.DictReader(io.StringIO(content))
    except csv.Error:
        return ImportPlan([], ["Could not read file"], 0)
    if reader.fieldnames is None:
        return ImportPlan([], ["CSV header is missing"], 0)

    seen = {identity_key(row["phone"], row["account"], row["profile_name"], row["start_date"], country_code)
            for row in existing}
    accepted = []
    errors = []
    duplicates = 0
    counted = 0
    for row_num, raw in enumerate(reader, start=2):
        counted += 1
        if counted > MAX_ROWS:
            errors.append(f"Row {row_num}: row limit of {MAX_ROWS} exceeded")
            break
        row = {}
        for key, value in raw.items():
            if key is None:
                continue
            row[key.strip().lower()] = (value or "").strip()

        def take(*keys: str) -> str:
            for key in keys:
                if key.lower() in row and row[key.lower()]:
                    return csv_unescape(row[key.lower()])
            return ""

        name = take("name")
        phone = take("phone", "whatsapp")
        account = take("account", "netflix account")
        profile = take("profile name", "profile_name", "profile")
        amount_raw = take("monthly amount", "monthly_amount", "amount") or "0"
        start_raw = take("start date", "start_date")
        payment = take("payment status", "payment_status") or "Payment pending"
        row_errors = []
        if not name:
            row_errors.append("name is required")
        if not phone:
            row_errors.append("phone is required")
        elif normalize_phone(phone, country_code) is None:
            row_errors.append("phone is invalid")
        if not account:
            row_errors.append("account is required")
        if not profile:
            row_errors.append("profile name is required")
        if not start_raw:
            row_errors.append("start date is required")
        for label, text in (("name", name), ("phone", phone), ("account", account), ("profile", profile)):
            if len(text) > MAX_FIELD:
                row_errors.append(f"{label} is too long")
        try:
            amount = float(amount_raw)
            if amount < 0 or amount != amount:  # NaN
                row_errors.append("amount is invalid")
        except ValueError:
            row_errors.append("amount is invalid")
            amount = 0.0
        if start_raw:
            try:
                date.fromisoformat(start_raw)
            except ValueError:
                row_errors.append("start date must be YYYY-MM-DD")
        if payment not in {"Paid", "Payment pending"}:
            row_errors.append("payment status must be Paid or Payment pending")
        if row_errors:
            errors.append(f"Row {row_num}: {'; '.join(row_errors)}")
            continue
        key = identity_key(phone, account, profile, start_raw, country_code)
        if key in seen:
            duplicates += 1
            errors.append(f"Row {row_num}: duplicate")
            continue
        seen.add(key)
        accepted.append({
            "name": name,
            "phone": phone.strip(),
            "account": account,
            "profile_name": profile,
            "monthly_amount": amount,
            "start_date": start_raw,
            "payment_status": payment,
        })
    return ImportPlan(accepted, errors, duplicates)
