"""Optional SMTP mailer for invitation emails.

When SMTP is configured (Security settings), invites are emailed; otherwise the
admin UI shows the invite link to share manually — appropriate for air-gapped
installs with no outbound mail.

Documented in: docs/architecture/incident-response-and-observability.md
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.services import settings

log = logging.getLogger("loc.mailer")

# H-2 (gate fix): the mailer is the TRUE header boundary. Every interpolated header value
# (subject, and any interpolated From/To display) is sanitized HERE, so no caller can leak
# a control char into a header — the per-caller hand-flattening in notifications.py was
# both incomplete (it collapsed only C0+DEL, missing U+0085/U+2028/U+2029) and applied at
# the wrong layer, so a project/org NAME with U+2028 reached the header and CPython's
# email policy raised OUTSIDE the try, dropping the mail install-wide and silently. The
# rejected set is exactly the chars a header value may not carry: the C0 controls, DEL, and
# the Unicode line/paragraph separators NEL / LS / PS (the set str.splitlines treats as line
# boundaries, which is what an SMTP header must never contain).
_HEADER_BAD = frozenset(range(0x00, 0x20)) | {0x7F, 0x85, 0x2028, 0x2029}


def _san(value: str) -> str:
    """Collapse any header-illegal char (C0/DEL/NEL/LS/PS) in an interpolated header
    value to a space, so the value can never break out of its header line."""
    if not value:
        return ""
    return "".join(" " if ord(c) in _HEADER_BAD else c for c in value)


def configured() -> bool:
    return bool(settings.get("smtp_host"))


def send(to: str, subject: str, body: str) -> bool:
    if not configured():
        return False
    # #520 gate: an app-written placeholder (a dedupe tombstone, an emailless account's
    # stand-in, a gdpr erasure tombstone) is never handed to the relay, even though its
    # `.invalid` domain would bounce there anyway.
    from app.services.security_client_ip_one_authority_ import is_placeholder_email

    if is_placeholder_email(to):
        return False
    host = settings.get("smtp_host")
    raw_port = settings.get("smtp_port") or "587"
    user = settings.get("smtp_user")
    pw = settings.read_secret_setting("smtp_password")  # Fernet-at-rest (R3), legacy-plaintext safe
    frm = settings.get("smtp_from") or user or "no-reply@localhost"
    try:
        # #454: the port is parsed INSIDE the try. The settings validator now refuses a
        # non-numeric smtp_port at every save door, but a row stored before it existed (or
        # written out of band) used to raise ValueError HERE, ahead of the try: `send` is
        # documented never to raise, so an invite request died as a 500 after its commit and
        # never reached its audit row, and every guarded caller dropped its mail in silence.
        port = int(raw_port)
        # H-2: build the message INSIDE the try, on SANITIZED header values, so a header
        # that the email policy still rejects is caught + logged, never a silent drop. The
        # BODY is set_content'd (not a header) and needs no header sanitization.
        msg = EmailMessage()
        msg["From"] = _san(frm)
        msg["To"] = _san(to)
        msg["Subject"] = _san(subject)
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=15) as srv:
            if settings.get("smtp_tls") == "1":
                srv.starttls(context=ssl.create_default_context())
            if user:
                srv.login(user, pw)
            srv.send_message(msg)
        return True
    except Exception:
        # never a raise: the caller's guarded tail already swallows, but a silent drop hid
        # real mail loss, so surface it.
        log.warning("mail send failed (to=%r subject=%r)", to, subject, exc_info=True)
        return False
