"""
Отправка email через SMTP (по умолчанию настроено под Gmail).

Переменные окружения:
    SMTP_HOST       — SMTP-сервер (по умолчанию smtp.gmail.com)
    SMTP_PORT       — порт (по умолчанию 587, STARTTLS)
    SMTP_USER       — логин SMTP (если не задан — берётся GARMIN_EMAIL из garmin-email)
    SMTP_PASSWORD   — пароль приложения Gmail (App Password; секрет smtp-password)
    MAIL_FROM       — адрес в поле From (по умолчанию = SMTP_USER / GARMIN_EMAIL)
    MAIL_TO         — получатель (если не задан — тот же GARMIN_EMAIL)

Для Gmail нужен App Password (https://myaccount.google.com/apppasswords),
обычный пароль аккаунта работать не будет при включённой 2FA.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any


def _smtp_user() -> str:
    """Gmail-адрес отправителя: SMTP_USER или тот же GARMIN_EMAIL (один секрет на всё)."""
    return (
        os.environ.get("SMTP_USER", "").strip()
        or os.environ.get("GARMIN_EMAIL", "").strip()
    )


def _recipients() -> list[str]:
    raw = (
        os.environ.get("MAIL_TO", "").strip()
        or _smtp_user()
    )
    return [a.strip() for a in raw.split(",") if a.strip()]


def is_configured() -> bool:
    """Заданы ли минимально необходимые параметры SMTP."""
    return bool(_smtp_user() and os.environ.get("SMTP_PASSWORD", "").strip() and _recipients())


def send_email(
    subject: str,
    html_body: str,
    text_body: str | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Отправляет письмо с HTML-телом (и текстовой альтернативой).
    attachments: [{"filename": "…", "content": "…", "content_type": "text/markdown"}]
    Возвращает {"ok": True, "to": [...]} или {"ok": False, "error": "..."}.
    """
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    try:
        port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    except ValueError:
        port = 587
    user = _smtp_user()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_from = os.environ.get("MAIL_FROM", "").strip() or user
    recipients = _recipients()

    if not user or not password:
        return {
            "ok": False,
            "error": "smtp_not_configured",
            "detail": "SMTP_PASSWORD missing (и нет SMTP_USER / GARMIN_EMAIL для отправителя)",
        }
    if not recipients:
        return {"ok": False, "error": "no_recipients", "detail": "MAIL_TO missing"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("MyBody", mail_from))
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body or "Откройте письмо в HTML-совместимом клиенте.")
    msg.add_alternative(html_body, subtype="html")

    attached_names: list[str] = []
    for att in attachments or []:
        filename = (att.get("filename") or "attachment.txt").strip()
        content = att.get("content") or ""
        if not content:
            continue
        ctype = (att.get("content_type") or "text/plain").strip().lower()
        if "markdown" in ctype or filename.endswith(".md"):
            maintype, subtype = "text", "plain"
        elif ctype.startswith("text/"):
            maintype, subtype = "text", ctype.split("/", 1)[1]
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            content.encode("utf-8"),
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
        attached_names.append(filename)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
        out: dict[str, Any] = {"ok": True, "to": recipients}
        if attached_names:
            out["attachments"] = attached_names
        return out
    except Exception as e:
        return {"ok": False, "error": "smtp_send_failed", "detail": str(e)}
