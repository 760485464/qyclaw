from __future__ import annotations

from email.mime.text import MIMEText
import smtplib

from backend.core.config import get_settings
from backend.i18n import t


class MailerError(Exception):
    pass


def send_password_reset_email(to_email: str, code: str, reset_link: str, expires_minutes: int = 10) -> None:
    settings = get_settings()
    smtp = settings.smtp

    if not smtp.enabled:
        raise MailerError("SMTP is disabled")

    subject = t("mailer.reset_subject")
    body = t("mailer.reset_body", code=code, reset_link=reset_link, minutes=expires_minutes)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{smtp.from_name} <{smtp.from_email}>"
    msg["To"] = to_email

    try:
        if smtp.use_ssl:
            with smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=smtp.timeout_seconds) as server:
                if smtp.username:
                    server.login(smtp.username, smtp.password)
                server.sendmail(smtp.from_email, [to_email], msg.as_string())
            return

        with smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout_seconds) as server:
            if smtp.use_tls:
                server.starttls()
            if smtp.username:
                server.login(smtp.username, smtp.password)
            server.sendmail(smtp.from_email, [to_email], msg.as_string())
    except Exception as exc:  # noqa: BLE001
        raise MailerError(f"SMTP send failed: {exc}") from exc
