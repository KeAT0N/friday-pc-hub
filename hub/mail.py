"""
hub/mail.py — IMAP/SMTP mail access for the Remote Hub (imaplib + smtplib).

Multi-provider (--provider icloud|gmail; icloud default). Stateless CLI,
standard JSON envelope. Credentials are pulled in-process from the keyring
backend (Windows Credential Manager) via credentials.py and never printed —
the password never leaves the process. Host/user/cred mapping per provider is
overridable via --user/--cred-service/--cred-account or HUB_MAIL_* env.

Containment
-----------
- Reads are bounded by a required-with-default integer limit (hard-capped) so
  a mailbox can never flood the orchestrator's context. IMAP mailbox is opened
  readonly and headers fetched with BODY.PEEK — reading never sets \\Seen.
- Sending is fail-closed: `send` validates + previews by default and only
  transmits with explicit --confirm-send. Body/subject are length-capped and
  every address is format-validated before any SMTP handshake.
- Kill-switch is asserted before any network handshake with iCloud.
- Every socket carries a timeout so a stalled server cannot hang the hub.

CLI:
    check                                       login round-trip, counts only
    mailboxes
    unread [--limit N]                          headers only, readonly
    read --uid U [--mailbox M] [--max-chars N]  one body, bounded, readonly
    send --to A --subject S --body B [--from F] [--confirm-send]
"""

from __future__ import annotations

import argparse
import email
import json
import os
import re
import sys
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage

import imaplib
import smtplib

try:  # package import or direct script run
    from hub.credentials import get_provider, CredentialError
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from credentials import get_provider, CredentialError
    from safety import KillSwitchEngaged, assert_alive

# Per-provider defaults. login_user / cred_account are overridable per call
# via CLI (--user, --cred-account, --cred-service) or env (HUB_MAIL_*).
PROVIDERS = {
    "icloud": {"imap": "imap.mail.me.com", "smtp": "smtp.mail.me.com",
               "smtp_port": 587, "user": "keatondavey@icloud.com",
               "cred_service": "icloud_mail", "cred_account": "keatondavey"},
    "gmail":  {"imap": "imap.gmail.com", "smtp": "smtp.gmail.com",
               "smtp_port": 587, "user": "cooldavey1256@gmail.com",
               "cred_service": "gmail", "cred_account": "keatondavey"},
}


@dataclass
class MailConfig:
    provider: str
    imap_host: str
    smtp_host: str
    smtp_port: int
    login_user: str
    cred_service: str
    cred_account: str


def resolve_config(args) -> MailConfig:
    base = PROVIDERS[args.provider]
    env = os.environ.get
    return MailConfig(
        provider=args.provider,
        imap_host=env("HUB_MAIL_IMAP", base["imap"]),
        smtp_host=env("HUB_MAIL_SMTP", base["smtp"]),
        smtp_port=int(env("HUB_MAIL_SMTP_PORT", base["smtp_port"])),
        login_user=args.user or env("HUB_MAIL_USER", base["user"]),
        cred_service=args.cred_service or env("HUB_MAIL_CRED_SERVICE",
                                              base["cred_service"]),
        cred_account=args.cred_account or env("HUB_MAIL_CRED_ACCOUNT",
                                              base["cred_account"]),
    )


CFG: MailConfig = None  # resolved from args in main() before dispatch

NET_TIMEOUT = 20.0
UNREAD_DEFAULT = 5
UNREAD_HARD_CAP = 25
BODY_READ_DEFAULT = 4_000
BODY_READ_HARD_CAP = 20_000
SEND_SUBJECT_MAX = 255
SEND_BODY_MAX = 10_000

# Deliberately strict, not RFC-complete: single address, no spaces, one @,
# a dotted domain. Rejects the header-injection vectors (newlines, commas).
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


class MailError(Exception):
    pass


def _password() -> str:
    """Fetch the app-specific password in-process. Value never emitted."""
    try:
        return get_provider("keyring").get(
            CFG.cred_service, CFG.cred_account).reveal()
    except CredentialError as e:
        raise MailError(f"credential unavailable: {e}") from e


def _valid_addr(addr: str) -> bool:
    return bool(EMAIL_RE.match(addr.strip()))


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


# ---------------------------------------------------------------- imap

def _imap_login() -> imaplib.IMAP4_SSL:
    assert_alive("mail.imap-connect")  # gate BEFORE the network handshake
    M = imaplib.IMAP4_SSL(CFG.imap_host, timeout=NET_TIMEOUT)
    try:
        M.login(CFG.login_user, _password())
    except imaplib.IMAP4.error as e:
        try:
            M.logout()
        except Exception:
            pass
        raise MailError(f"IMAP login failed: {e}")
    return M


def do_check(args) -> None:
    M = _imap_login()
    try:
        typ, data = M.select("INBOX", readonly=True)
        total = int(data[0]) if typ == "OK" and data and data[0] else 0
    finally:
        M.logout()
    emit(True, "check", data={"provider": CFG.provider, "host": CFG.imap_host,
                              "user": CFG.login_user, "login": "ok",
                              "inbox_total": total})


def do_mailboxes(args) -> None:
    M = _imap_login()
    try:
        typ, boxes = M.list()
        names = []
        for b in boxes or []:
            line = b.decode(errors="replace") if isinstance(b, bytes) else str(b)
            names.append(line.split(' "/" ')[-1].strip('"'))
    finally:
        M.logout()
    emit(True, "mailboxes", data={"count": len(names), "mailboxes": names})


def do_unread(args) -> None:
    limit = max(1, min(args.limit, UNREAD_HARD_CAP))
    M = _imap_login()
    try:
        M.select("INBOX", readonly=True)  # readonly: cannot set \Seen
        typ, data = M.uid("search", None, "UNSEEN")
        uids = data[0].split() if data and data[0] else []
        total_unread = len(uids)
        chosen = uids[-limit:][::-1]  # newest first
        headers = []
        for uid in chosen:
            assert_alive("mail.unread-fetch")
            typ, msgdata = M.uid(
                "fetch", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = next((p[1] for p in msgdata if isinstance(p, tuple)), b"")
            msg = email.message_from_bytes(raw)
            headers.append({
                "uid": uid.decode(),
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")),
                "date": _decode(msg.get("Date")),
            })
    finally:
        M.logout()
    emit(True, "unread", data={
        "total_unread": total_unread, "returned": len(headers),
        "limit": limit, "capped": total_unread > limit, "headers": headers,
    })


def _extract_text(msg, cap: int) -> tuple[str, bool, list[str]]:
    text, attachments = "", []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp.lower():
            attachments.append(_decode(part.get_filename()) or "(unnamed)")
            continue
        if part.get_content_type() == "text/plain" and not text:
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
    return text[:cap], len(text) > cap, attachments


def do_read(args) -> None:
    cap = max(1, min(args.max_chars, BODY_READ_HARD_CAP))
    M = _imap_login()
    try:
        M.select(args.mailbox, readonly=True)
        typ, msgdata = M.uid("fetch", args.uid.encode(), "(BODY.PEEK[])")
        raw = next((p[1] for p in msgdata if isinstance(p, tuple)), None)
        if raw is None:
            emit(False, "read", error=f"uid {args.uid} not found in {args.mailbox}")
        msg = email.message_from_bytes(raw)
        body, truncated, attachments = _extract_text(msg, cap)
    finally:
        M.logout()
    emit(True, "read", data={
        "uid": args.uid, "mailbox": args.mailbox,
        "from": _decode(msg.get("From")), "to": _decode(msg.get("To")),
        "subject": _decode(msg.get("Subject")), "date": _decode(msg.get("Date")),
        "body_chars": len(body), "truncated": truncated,
        "attachments": attachments, "body": body,
    })


def do_send(args) -> None:
    from_addr = (args.from_addr or CFG.login_user).strip()
    to_addr = args.to.strip()
    errors = []
    if not _valid_addr(to_addr):
        errors.append(f"invalid --to address {to_addr!r}")
    if not _valid_addr(from_addr):
        errors.append(f"invalid --from address {from_addr!r}")
    if len(args.subject) > SEND_SUBJECT_MAX:
        errors.append(f"subject exceeds {SEND_SUBJECT_MAX} chars")
    if len(args.body) > SEND_BODY_MAX:
        errors.append(f"body exceeds {SEND_BODY_MAX} chars")
    if "\n" in args.subject or "\r" in args.subject:
        errors.append("subject contains newline (header-injection guard)")
    if errors:
        emit(False, "send", error="; ".join(errors))

    preview = {"from": from_addr, "to": to_addr, "subject": args.subject,
               "body_chars": len(args.body)}

    if not args.confirm_send:
        emit(True, "send", data={**preview, "sent": False, "dry_run": True,
             "note": "validated only; pass --confirm-send to transmit"})

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = args.subject
    msg.set_content(args.body)

    assert_alive("mail.smtp-connect")  # gate BEFORE the network handshake
    try:
        with smtplib.SMTP(CFG.smtp_host, CFG.smtp_port, timeout=NET_TIMEOUT) as s:
            s.starttls()
            s.login(CFG.login_user, _password())
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        emit(False, "send", data=preview, error=f"SMTP error: {type(e).__name__}: {e}")
    emit(True, "send", data={**preview, "sent": True, "dry_run": False})


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="hub.mail", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)  # shared by all subcommands
    common.add_argument("--provider", choices=sorted(PROVIDERS),
                        default="icloud")
    common.add_argument("--user", help="override login/from address")
    common.add_argument("--cred-service")
    common.add_argument("--cred-account")

    sub.add_parser("check", parents=[common])
    sub.add_parser("mailboxes", parents=[common])

    u = sub.add_parser("unread", parents=[common])
    u.add_argument("--limit", type=int, default=UNREAD_DEFAULT)

    r = sub.add_parser("read", parents=[common])
    r.add_argument("--uid", required=True)
    r.add_argument("--mailbox", default="INBOX")
    r.add_argument("--max-chars", type=int, default=BODY_READ_DEFAULT)

    s = sub.add_parser("send", parents=[common])
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--from", dest="from_addr")
    s.add_argument("--confirm-send", action="store_true")

    args = p.parse_args()
    global CFG
    CFG = resolve_config(args)
    handler = {"check": do_check, "mailboxes": do_mailboxes, "unread": do_unread,
               "read": do_read, "send": do_send}[args.action]
    try:
        assert_alive(f"mail.{args.action}")
        handler(args)
    except (MailError, KillSwitchEngaged) as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
