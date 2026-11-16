"""Outgoing email through the SMTP settings of the administration panel."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from flask import current_app

from . import settings


def smtp_configured() -> bool:
    return bool(str(settings.get("smtp_host")).strip())


def send_mail(recipient: str, subject: str, body: str) -> bool:
    """Send a plain-text message; return False when SMTP is not configured.

    In testing mode messages are kept in ``app.extensions["dagonweb_outbox"]``.
    """
    if current_app.config.get("TESTING"):
        current_app.extensions.setdefault("dagonweb_outbox", []).append({"to": recipient, "subject": subject, "body": body})
        return True
    host = str(settings.get("smtp_host")).strip()
    if not host:
        current_app.logger.warning("SMTP is not configured; message to %s not sent: %s", recipient, subject)
        return False
    message = EmailMessage()
    message["From"] = settings.get("smtp_from") or "noreply@localhost"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(host, int(settings.get("smtp_port") or 587), timeout=15) as smtp:
        if settings.get("smtp_tls"):
            smtp.starttls()
        username = str(settings.get("smtp_user") or "")
        if username:
            smtp.login(username, str(settings.get("smtp_password") or ""))
        smtp.send_message(message)
    return True
