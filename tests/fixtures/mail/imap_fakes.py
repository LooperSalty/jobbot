"""Test doubles for the inbox tests: raw e-mail builder + a Gmail-like fake IMAP server (no network)."""

from __future__ import annotations

import hashlib
import imaplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

ME = "jean.dupont@example.com"
ALL = "[Gmail]/Tous les messages"
SPAM = "[Gmail]/Spam"


def make_raw(*, frm="Jane Doe <jane@acme-logiciels.fr>", subject="Re: Candidature", body="Bonjour",
             in_reply_to=None, headers: dict | None = None, date: datetime | None = None, html: str | None = None,
             msgid: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = ME
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date or datetime(2026, 9, 24, 9, 0, tzinfo=UTC))
    digest = hashlib.sha1(f"{frm}|{subject}|{body}".encode()).hexdigest()[:16]
    msg["Message-ID"] = msgid or f"<{digest}@test>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def _header_block(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in raw:
            return raw.split(sep, 1)[0] + sep
    return raw


class FakeIMAP:
    """Minimal imaplib.IMAP4 stand-in with Gmail-like LIST / SELECT / UID SEARCH / UID FETCH."""

    def __init__(self, folders: dict[str, dict], *, password_ok: bool = True, gmail: bool = True):
        self.folders = folders  # name -> {"flag": "\\All", "uidvalidity": 1, "messages": {uid: (raw, thrid)}}
        self.password_ok = password_ok
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.selected: str | None = None
        self.commands: list[tuple] = []
        self.logged_out = False
        self._untagged: dict[str, list] = {}

    def login(self, user, password):
        if not self.password_ok:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        assert " " not in password
        return "OK", [b"Logged in"]

    def list(self, directory='""', pattern="*"):
        lines: list = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Noselect \\HasChildren) "/" "[Gmail]"']
        for name, spec in self.folders.items():
            if spec.get("literal"):
                lines.append((f'(\\HasNoChildren {spec["flag"]}) "/" {{{len(name)}}}'.encode(), name.encode()))
            else:
                lines.append(f'(\\HasNoChildren {spec["flag"]}) "/" "{name}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        assert readonly, "folders must be opened read-only (EXAMINE)"
        name = mailbox.strip('"')
        self.selected = name
        self._untagged = {"UIDVALIDITY": [str(self.folders[name]["uidvalidity"]).encode()]}
        return "OK", [str(len(self.folders[name]["messages"])).encode()]

    def response(self, code):
        return code, self._untagged.pop(code, [None])

    def uid(self, command, *args):
        self.commands.append((command, *args))
        messages = self.folders[self.selected]["messages"]
        if command == "SEARCH":
            if args[0] == "UID":
                start = int(args[1].split(":")[0])
                # like Gmail: "UID n:*" returns at least the last message even when n > max UID
                found = [u for u in messages if u >= start] or ([max(messages)] if messages else [])
            else:
                found = list(messages)
            return "OK", [" ".join(str(u) for u in sorted(found)).encode()]
        if command == "FETCH":
            spec = args[1]
            assert "RFC822)" not in spec and "BODY[]" not in spec.replace("BODY.PEEK[]", ""), "must use PEEK"
            return "OK", self._fetch([int(u) for u in args[0].split(",")], spec)
        raise AssertionError(command)

    def _fetch(self, uids, spec):
        data: list = []
        for n, uid in enumerate(uids, start=1):
            raw, thrid = self.folders[self.selected]["messages"][uid]
            if "HEADER" in spec:
                payload = _header_block(raw)
                meta = f"{n} (UID {uid} X-GM-THRID {thrid} RFC822.SIZE {len(raw)} BODY[HEADER] {{{len(payload)}}}"
            else:
                payload = raw
                meta = f"{n} (UID {uid} BODY[] {{{len(payload)}}}"
            data.extend([(meta.encode(), payload), b")"])
        return data

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


def gmail(all_msgs: dict, spam_msgs: dict | None = None, **kwargs) -> FakeIMAP:
    """French-UI Gmail: '[Gmail]/Tous les messages' (\\All) + '[Gmail]/Spam' (\\Junk, name sent as a literal)."""
    return FakeIMAP({
        ALL: {"flag": "\\All", "uidvalidity": 11, "messages": all_msgs},
        SPAM: {"flag": "\\Junk", "uidvalidity": 22, "messages": spam_msgs or {}, "literal": True},
    }, **kwargs)
