"""Telegram Bot API calls. Tokens stay in the request and out of exceptions."""

from __future__ import annotations

import json
import re
from urllib import error, request

class TelegramError(Exception):
    pass


_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


def _opener(opener, req, timeout):
    open_fn = opener or request.urlopen
    return open_fn(req, timeout=timeout)


def send_message(token: str, chat_id: int, text: str, buttons=None, opener=None) -> None:
    if type(chat_id) is not int or chat_id <= 0:
        raise TelegramError("refusing destination")
    if not token or any(ch.isspace() for ch in token):
        raise TelegramError("telegram is not configured")
    body = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    _post_bot(token, "sendMessage", body, opener)


def send_document(token: str, chat_id: int, filename: str, data: bytes, opener=None) -> None:
    if type(chat_id) is not int or chat_id <= 0:
        raise TelegramError("refusing destination")
    if not filename or "/" in filename or "\\" in filename or len(data) > 200_000:
        raise TelegramError("refusing document")
    boundary = "nmBoundary7f3a"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + data + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    payload = b"".join(parts)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = request.Request(
        url,
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    _execute(req, opener, token)


def answer_callback(token: str, callback_id: str, text: str, opener=None) -> None:
    if not callback_id or len(callback_id) > 200:
        return
    _post_bot(token, "answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text[:180],
    }, opener)


def fetch_file(token: str, file_id: str, opener=None, max_bytes: int = 200_000) -> bytes:
    if not file_id or len(file_id) > 200 or any(ch.isspace() for ch in file_id):
        raise TelegramError("refusing file")
    meta = _post_bot(token, "getFile", {"file_id": file_id}, opener, decode=True)
    path = ((meta or {}).get("result") or {}).get("file_path", "")
    if not _SAFE_PATH.fullmatch(path) or ".." in path or path.startswith("/"):
        raise TelegramError("refusing file path")
    url = f"https://api.telegram.org/file/bot{token}/{path}"
    req = request.Request(url, method="GET")
    payload = _execute(req, opener, token, decode=False)
    if len(payload) > max_bytes:
        raise TelegramError("file is too large")
    return payload


def _post_bot(token, method, body, opener, decode=False):
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    raw = _execute(req, opener, token)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        raise TelegramError("telegram response was not json") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise TelegramError("telegram rejected the message")
    return payload if decode else None


def _execute(req, opener, token, decode=False):
    try:
        with _opener(opener, req, 8) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read() if hasattr(response, "read") else b""
            if status != 200:
                raise TelegramError(f"telegram status {status}")
            return raw if raw is not None else b"{}"
    except TelegramError:
        raise
    except error.HTTPError as exc:
        raise TelegramError(f"telegram status {exc.code}") from None
    except Exception:
        raise TelegramError("telegram request failed") from None
