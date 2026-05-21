from __future__ import annotations

import json
import logging
import urllib.request
import uuid

logger = logging.getLogger(__name__)


def _call_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    except Exception:
        logger.exception("Telegram API call failed: %s", method)
        return {}


def send_message(token: str, chat_id: int, text: str, parse_mode: str = "HTML") -> dict:
    return _call_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


def send_message_with_keyboard(
    token: str,
    chat_id: int,
    text: str,
    keyboard: list[list[dict]],
    parse_mode: str = "HTML",
) -> dict:
    return _call_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": {"inline_keyboard": keyboard},
        },
    )


def edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: list[list[dict]] | None = None,
    parse_mode: str = "HTML",
) -> dict:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _call_api(token, "editMessageText", payload)


def answer_callback(token: str, callback_query_id: str, text: str = "") -> dict:
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _call_api(token, "answerCallbackQuery", payload)


def delete_message(token: str, chat_id: int, message_id: int) -> dict:
    return _call_api(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def build_keyboard(buttons: list[tuple[str, str]], columns: int = 2) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for i in range(0, len(buttons), columns):
        row = [{"text": label, "callback_data": data} for label, data in buttons[i : i + columns]]
        rows.append(row)
    return rows


def _build_multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    mime_type: str,
) -> tuple[str, bytes]:
    boundary = f"----TGBotFormBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode())
    parts.append(f"Content-Type: {mime_type}\r\n\r\n".encode())
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(parts)


def send_document(
    token: str,
    chat_id: int,
    filename: str,
    content: bytes,
    mime_type: str = "text/csv",
    caption: str = "",
) -> dict:
    fields: dict[str, str] = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    boundary, body = _build_multipart(fields, "document", filename, content, mime_type)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
    except Exception:
        logger.exception("Telegram API call failed: sendDocument")
        return {}
