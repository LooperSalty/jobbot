"""Gmail IMAP reading: special-use folders (\\All, \\Junk), incremental UID scan, read-only fetches.

Research: docs/research/email-sending-inbox.md §2.2, §3.2, §5 (7-10), §6.3-6.5.
Never marks mail as read: folders are EXAMINEd (readonly) and bodies fetched with BODY.PEEK.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobbot import clock
from jobbot.config import Secrets
from jobbot.mail.mailer import MailError
from jobbot.mail.messages import InboundMessage
from jobbot.mail.parsing import parse_message, sender_address

log = logging.getLogger(__name__)

SCAN_FLAGS = ("\\All", "\\Junk")
FIRST_RUN_DAYS = 14  # first scan (or UIDVALIDITY change): messages of the last 14 days
FETCH_CHUNK = 50
MAX_BODY_BYTES = 8 * 1024 * 1024  # bigger messages are read from their headers only
IMAP_TIMEOUT_S = 30
HEADER_SPEC_GMAIL = "(UID X-GM-THRID RFC822.SIZE BODY.PEEK[HEADER])"
HEADER_SPEC = "(UID RFC822.SIZE BODY.PEEK[HEADER])"
BODY_SPEC = "(UID BODY.PEEK[])"

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$', re.IGNORECASE)
_LITERAL_TAIL = re.compile(rb"\{\d+\}\s*$")
_UID_RE = re.compile(rb"\bUID (\d+)")
_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ImapFactory = Callable[[str, int], Any]


@dataclass(frozen=True)
class FolderCursor:
    """Incremental position in one folder. last_uid is only meaningful for its uidvalidity."""

    uidvalidity: int | None = None
    last_uid: int | None = None


@dataclass(frozen=True)
class ScanResult:
    messages: tuple[InboundMessage, ...] = ()
    own_messages: tuple[InboundMessage, ...] = ()  # our sent copies (headers only): thread mapping
    cursors: dict[str, FolderCursor] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Fetched:
    uid: int
    thread_id: str | None
    size: int
    payload: bytes


# --------------------------------------------------------------------------- connection & folders


def quote_mailbox(name: str) -> str:
    """imaplib does not quote mailbox names ('[Gmail]/Tous les messages' has spaces)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _default_factory(host: str, port: int) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT_S)


def open_imap(secrets: Secrets, imap_factory: ImapFactory | None = None) -> Any:
    user = (secrets.smtp_user or "").strip()
    password = (secrets.smtp_password or "").replace(" ", "").strip()
    if not user or not password:
        raise MailError("IMAP non configuré : renseigne SMTP_USER et SMTP_PASSWORD (mot de passe d'application).")
    try:
        imap = (imap_factory or _default_factory)(secrets.imap_host, int(secrets.imap_port))
    except (OSError, imaplib.IMAP4.error) as exc:
        raise MailError(f"Serveur IMAP injoignable ({secrets.imap_host}) : {exc}") from exc
    try:
        imap.login(user, password)
    except (imaplib.IMAP4.error, OSError) as exc:
        close_quietly(imap)
        raise MailError(
            "Connexion IMAP refusée : vérifie SMTP_USER et le mot de passe d'application Gmail (16 lettres)."
        ) from exc
    return imap


def close_quietly(imap: Any) -> None:
    try:
        imap.logout()
    except Exception:  # connection already dropped: nothing to do
        log.debug("Déconnexion IMAP ignorée", exc_info=True)


def _list_line(line: bytes | tuple) -> bytes:
    if isinstance(line, tuple):  # mailbox name sent as a literal: (b'(\\All) "/" {17}', b'name')
        meta, name = bytes(line[0]), bytes(line[1] or b"")
        return _LITERAL_TAIL.sub(b"", meta) + b'"' + name.replace(b'"', b'\\"') + b'"'
    return bytes(line or b"")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def special_folders(imap: Any) -> dict[str, str]:
    """{'\\All': '[Gmail]/Tous les messages', '\\Junk': '[Gmail]/Spam', ...} from LIST flags (RFC 6154)."""
    typ, lines = imap.list()
    if typ != "OK":
        return {}
    found: dict[str, str] = {}
    for line in lines or []:
        match = _LIST_RE.match(_list_line(line).strip())
        if not match:
            continue
        name = _unquote(match.group("name").decode("utf-8", "replace"))
        for flag in match.group("flags").decode("ascii", "replace").split():
            found.setdefault("\\" + flag.lstrip("\\").capitalize(), name)  # '\\JUNK' → '\\Junk'
    return found


def _is_gmail(imap: Any) -> bool:
    caps = getattr(imap, "capabilities", ()) or ()
    return any(str(c).upper() == "X-GM-EXT-1" for c in caps)


# --------------------------------------------------------------------------- search & fetch


def _imap_date(dt: datetime) -> str:
    d = clock.to_paris(dt)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _select(imap: Any, folder: str) -> int | None:
    typ, data = imap.select(quote_mailbox(folder), readonly=True)
    if typ != "OK":
        raise MailError(f"Dossier IMAP inaccessible : {folder} ({data})")
    try:
        _, values = imap.response("UIDVALIDITY")
        return int(values[0]) if values and values[0] is not None else None
    except (TypeError, ValueError, IndexError, imaplib.IMAP4.error):
        return None


def _search_uids(imap: Any, last_uid: int | None) -> list[int]:
    if last_uid is None:
        typ, data = imap.uid("SEARCH", "SINCE", _imap_date(clock.now() - timedelta(days=FIRST_RUN_DAYS)))
    else:
        typ, data = imap.uid("SEARCH", "UID", f"{last_uid + 1}:*")
    if typ != "OK":
        raise MailError(f"Recherche IMAP refusée : {data}")
    tokens = b" ".join(d for d in (data or []) if isinstance(d, bytes)).split()
    uids = {int(t) for t in tokens if t.isdigit()}
    # "UID n:*" always returns at least the last message, even when n > max UID (research §5.8)
    return sorted(u for u in uids if last_uid is None or u > last_uid)


def _chunks(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _parse_fetch(data: list) -> list[_Fetched]:
    records: list[tuple[bytes, bytes]] = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            records.append((bytes(item[0]), bytes(item[1] or b"")))
        elif isinstance(item, bytes) and records:  # attributes sent after the literal: b' X-GM-THRID 1)'
            meta, payload = records[-1]
            records[-1] = (meta + b" " + item, payload)
    fetched: list[_Fetched] = []
    for meta, payload in records:
        uid = _UID_RE.search(meta)
        if uid is None:
            continue
        thrid = _THRID_RE.search(meta)
        size = _SIZE_RE.search(meta)
        fetched.append(
            _Fetched(
                uid=int(uid.group(1)),
                thread_id=thrid.group(1).decode() if thrid else None,
                size=int(size.group(1)) if size else len(payload),
                payload=payload,
            )
        )
    return fetched


def _fetch(imap: Any, uids: list[int], spec: str) -> list[_Fetched]:
    out: list[_Fetched] = []
    for chunk in _chunks(uids, FETCH_CHUNK):
        typ, data = imap.uid("FETCH", ",".join(str(u) for u in chunk), spec)
        if typ != "OK":
            log.warning("FETCH IMAP refusé pour %d message(s) : %s", len(chunk), data)
            continue
        out.extend(_parse_fetch(data))
    return out


def _to_message(item: _Fetched, raw: bytes, folder: str) -> InboundMessage | None:
    try:
        return parse_message(raw, uid=item.uid, folder=folder, gm_thread_id=item.thread_id)
    except Exception:  # one unreadable e-mail never stops the scan
        log.warning("Message IMAP %s/%d illisible, ignoré", folder, item.uid, exc_info=True)
        return None


# --------------------------------------------------------------------------- scan


def _scan_folder(
    imap: Any, folder: str, cursor: FolderCursor, own: frozenset[str], limit: int, gmail: bool
) -> tuple[list[InboundMessage], list[InboundMessage], FolderCursor]:
    uidvalidity = _select(imap, folder)
    same_epoch = cursor.uidvalidity is None or uidvalidity is None or cursor.uidvalidity == uidvalidity
    last = cursor.last_uid if same_epoch else None
    if not same_epoch:
        log.info("UIDVALIDITY de %s a changé : nouveau balayage des %d derniers jours", folder, FIRST_RUN_DAYS)
    uids = _search_uids(imap, last)[: max(0, limit)]
    heads = _fetch(imap, uids, HEADER_SPEC_GMAIL if gmail else HEADER_SPEC)
    own_uids = {h.uid for h in heads if sender_address(h.payload) in own}
    own_heads = [h for h in heads if h.uid in own_uids]
    other_heads = [h for h in heads if h.uid not in own_uids]
    wanted = [h.uid for h in other_heads if h.size <= MAX_BODY_BYTES]
    bodies = {f.uid: f.payload for f in _fetch(imap, wanted, BODY_SPEC)} if wanted else {}
    messages = [_to_message(h, bodies.get(h.uid, h.payload), folder) for h in other_heads]
    mine = [_to_message(h, h.payload, folder) for h in own_heads]
    new_last = max(uids) if uids else last
    return (
        [m for m in messages if m is not None],
        [m for m in mine if m is not None],
        FolderCursor(uidvalidity=uidvalidity, last_uid=new_last),
    )


def _targets(folders: dict[str, str]) -> list[tuple[str, str]]:
    targets = [(flag, folders[flag]) for flag in SCAN_FLAGS if flag in folders]
    if "\\All" not in folders:  # not Gmail (or LIST failed): read the inbox at least
        targets.insert(0, ("\\All", "INBOX"))
    return targets


def scan_mailbox(
    secrets: Secrets,
    cursors: dict[str, FolderCursor],
    *,
    imap_factory: ImapFactory | None = None,
    limit: int = 200,
    own_addresses: frozenset[str] = frozenset(),
) -> ScanResult:
    """Scan \\All and \\Junk from their cursors. Raises MailError only if the connection/login fails."""
    imap = open_imap(secrets, imap_factory)
    messages: list[InboundMessage] = []
    own: list[InboundMessage] = []
    new_cursors = dict(cursors)
    errors: list[str] = []
    try:
        gmail = _is_gmail(imap)
        for flag, folder in _targets(special_folders(imap)):
            try:
                found, mine, cursor = _scan_folder(
                    imap, folder, cursors.get(flag, FolderCursor()), own_addresses, limit, gmail
                )
            except (MailError, imaplib.IMAP4.error, OSError) as exc:
                log.warning("Lecture du dossier %s impossible : %s", folder, exc)
                errors.append(f"Dossier {folder} non lu : {exc}")
                continue
            messages.extend(found)
            own.extend(mine)
            new_cursors[flag] = cursor
    finally:
        close_quietly(imap)
    return ScanResult(tuple(messages), tuple(own), new_cursors, tuple(errors))
