"""Outgoing e-mail (Gmail SMTP + app password), threading-aware, header-injection safe.

Research: docs/research/email-sending-inbox.md §2.1, §3.3, §5, §6.2.
"""

from __future__ import annotations

import contextlib
import html
import logging
import mimetypes
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from jobbot import clock
from jobbot.config import Profile, Secrets
from jobbot.db import Repository
from jobbot.models import SentEmail
from jobbot.textutil import slugify, strip_accents

if TYPE_CHECKING:
    from jobbot.context import AppContext

log = logging.getLogger(__name__)

MSGID_DOMAIN = "gmail.com"
MSGID_IDSTRING = "jobbot"
WARN_ATTACH_BYTES = 2 * 1024 * 1024  # research: one PDF ≤ 1-2 MB
MAX_ATTACH_BYTES = 18 * 1024 * 1024  # Gmail refuses > 25 MB once base64-encoded
MAX_REFERENCES = 20
SMTP_TIMEOUT_S = 30
SUBJECT_KV_PREFIX = "mail:subject:"  # kv key storing the subject of every sent Message-ID (threading)

_RE_PREFIX = re.compile(r"^\s*((re|tr|fw|fwd|aw|wg|sv|rif|ref)\s*:\s*)+", re.IGNORECASE)
_ADDR_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}$")
_MSGID_RE = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")
_CRLF = re.compile(r"[\r\n\x00]+")


class MailError(RuntimeError):
    """Sending failed or was refused (French, user-facing message)."""


class MailFatalError(MailError):
    """The whole e-mail channel must stop for this run (bad credentials, Gmail quota, spam block)."""


class OutgoingEmail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str
    subject: str
    body: str
    attachments: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    # report = weekly report sent to oneself; notification = important notification e-mailed to oneself
    kind: Literal["application", "spontaneous", "followup", "report", "notification"]
    application_id: int | None = None


# --------------------------------------------------------------------------- pure helpers


def reply_subject(subject: str) -> str:
    """'RE : TR: Candidature' → 'Re: Candidature' (never 'Re: Re:')."""
    return f"Re: {_RE_PREFIX.sub('', clean_header(subject)).strip()}"


def clean_header(value: str) -> str:
    """Remove CR/LF/NUL (header injection) and collapse whitespace."""
    return re.sub(r"\s+", " ", _CRLF.sub(" ", value or "")).strip()


def is_valid_address(address: str) -> bool:
    return bool(_ADDR_RE.match(address or "")) and ".." not in address


def parse_recipient(raw: str) -> tuple[str, str]:
    """'Mme X <x@y.fr>' → ('Mme X', 'x@y.fr'); exactly one valid address or MailError."""
    if _CRLF.search(raw or ""):
        raise MailError(f"Adresse destinataire invalide (retour à la ligne interdit) : {raw!r}")
    pairs = [(n, a) for n, a in getaddresses([raw or ""]) if a]
    if len(pairs) != 1:
        raise MailError(f"Un seul destinataire valide attendu : {raw!r}")
    name, addr = pairs[0]
    addr = addr.strip()
    if not is_valid_address(addr):
        raise MailError(f"Adresse e-mail invalide : {addr!r}")
    return clean_header(name), addr.lower()


def address_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in (address or "") else ""


def normalize_msgid(value: str | None) -> str | None:
    """Keep a Message-ID only if it is a single well-formed '<local@domain>' token."""
    if not value:
        return None
    token = clean_header(value)
    if token and not token.startswith("<"):
        token = f"<{token}>"
    return token if _MSGID_RE.match(token) else None


def ascii_filename(name: str) -> str:
    """'CV_Jean_Développeur (1).pdf' → 'CV_Jean_Developpeur_1.pdf' (ASCII, no spaces)."""
    path = Path(name or "piece_jointe")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", strip_accents(path.stem)).strip("._-")
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix.lower())
    return f"{re.sub(r'_+', '_', stem) or 'piece_jointe'}{suffix}"


def html_alternative(text: str) -> str:
    """Minimal HTML twin of the plain-text body (no images, no tracking, no styling tricks)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    inner = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return (
        '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"></head>'
        f'<body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">{inner}</body></html>'
    )


def _thread_headers(email: OutgoingEmail) -> tuple[str | None, list[str]]:
    in_reply_to = normalize_msgid(email.in_reply_to)
    if email.in_reply_to and in_reply_to is None:
        log.warning("In-Reply-To invalide ignoré : %r", email.in_reply_to)
    chain: list[str] = []
    for ref in email.references:
        mid = normalize_msgid(ref)
        if mid and mid not in chain:
            chain.append(mid)
    if in_reply_to and (not chain or chain[-1] != in_reply_to):
        chain = [m for m in chain if m != in_reply_to] + [in_reply_to]
    return in_reply_to, chain[-MAX_REFERENCES:]


def _read_attachments(paths: list[str]) -> list[tuple[str, bytes, str, str]]:
    """(filename, data, maintype, subtype) for each file; MailError if missing or too large."""
    out: list[tuple[str, bytes, str, str]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise MailError(f"Pièce jointe introuvable : {path.name}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MailError(f"Pièce jointe illisible ({path.name}) : {exc}") from exc
        mime = "application/pdf" if path.suffix.lower() == ".pdf" else mimetypes.guess_type(path.name)[0]
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        out.append((ascii_filename(path.name), data, maintype, subtype))
    total = sum(len(d) for _, d, _, _ in out)
    if total > MAX_ATTACH_BYTES:
        raise MailError(f"Pièces jointes trop lourdes ({total // 1024} Ko) : réduis la taille du CV.")
    if total > WARN_ATTACH_BYTES:
        log.warning("Pièces jointes volumineuses (%d Ko > 2 Mo) : un CV de 1 Mo maximum est conseillé.", total // 1024)
    return out


# --------------------------------------------------------------------------- mailer


class Mailer:
    """Builds and sends one-to-one e-mails. One SMTP session per message (Gmail drops idle sessions)."""

    def __init__(
        self,
        secrets: Secrets,
        repo: Repository,
        profile: Profile,
        *,
        dry_run: bool = False,
        outbox_dir: Path | None = None,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ):
        self.secrets = secrets
        self.repo = repo
        self.profile = profile
        self.dry_run = dry_run
        self.outbox_dir = Path(outbox_dir) if outbox_dir else Path("output") / "outbox"
        self._smtp_factory = smtp_factory
        self.sender = (secrets.smtp_user or profile.identity.email or "").strip().lower()

    # ------------------------------------------------------------------ public

    def send(self, email: OutgoingEmail) -> SentEmail:
        to_name, to_addr = parse_recipient(email.to)
        if self.repo.is_blacklisted(to_addr, address_domain(to_addr)):
            raise MailError(f"Destinataire en liste noire, envoi refusé : {to_addr}")
        msg = self.build_message(email, to_name, to_addr)
        if self.dry_run:
            path = self._write_eml(msg, email.kind, to_addr)
            log.info("[simulation] e-mail %s pour %s écrit dans %s", email.kind, to_addr, path)
        else:
            self._smtp_send(msg, to_addr)
            log.info("E-mail %s envoyé à %s (%s)", email.kind, to_addr, msg["Message-ID"])
        return self._log_sent(email, msg, to_addr)

    def build_message(self, email: OutgoingEmail, to_name: str, to_addr: str) -> EmailMessage:
        if not is_valid_address(self.sender):
            raise MailError("Adresse d'expédition absente : renseigne SMTP_USER dans .env.")
        in_reply_to, chain = _thread_headers(email)
        subject = reply_subject(email.subject) if in_reply_to else clean_header(email.subject)
        if not subject:
            raise MailError("Objet de l'e-mail vide.")
        msg = EmailMessage(policy=SMTP_POLICY)
        msg["From"] = formataddr((clean_header(self.profile.identity.full_name), self.sender))
        msg["To"] = formataddr((to_name, to_addr)) if to_name else to_addr
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(idstring=MSGID_IDSTRING, domain=address_domain(self.sender) or MSGID_DOMAIN)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = " ".join(chain)
        body = (email.body or "").replace("\r\n", "\n").strip() + "\n"
        msg.set_content(body, cte="quoted-printable")
        msg.add_alternative(html_alternative(body), subtype="html", cte="quoted-printable")
        for filename, data, maintype, subtype in _read_attachments(email.attachments):
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    # ------------------------------------------------------------------ internals

    def _log_sent(self, email: OutgoingEmail, msg: EmailMessage, to_addr: str) -> SentEmail:
        sent = SentEmail(
            to=to_addr,
            subject=str(msg["Subject"]),
            message_id=str(msg["Message-ID"]),
            in_reply_to=str(msg["In-Reply-To"]) if msg["In-Reply-To"] else None,
            kind=email.kind,
            application_id=email.application_id,
            sent_at=clock.now(),
            dry_run=self.dry_run,
        )
        try:
            self.repo.log_sent_email(sent)
            self.repo.kv_set(SUBJECT_KV_PREFIX + sent.message_id, sent.subject)
        except Exception:  # the e-mail is gone: never turn a DB hiccup into a "not sent"
            log.exception("E-mail envoyé mais non journalisé : %s", sent.message_id)
        return sent

    def _write_eml(self, msg: EmailMessage, kind: str, to_addr: str) -> Path:
        stamp = clock.to_paris(clock.now()).strftime("%Y%m%d-%H%M%S")
        token = slugify(str(msg["Message-ID"]), 12)
        path = self.outbox_dir / f"{stamp}-{kind}-{slugify(to_addr, 40)}-{token}.eml"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(msg.as_bytes())
        except OSError as exc:
            raise MailError(f"Impossible d'écrire le brouillon {path.name} : {exc}") from exc
        return path

    def _credentials(self) -> tuple[str, str]:
        user = (self.secrets.smtp_user or "").strip()
        password = (self.secrets.smtp_password or "").replace(" ", "").strip()
        if not user or not password:
            raise MailFatalError(
                "SMTP non configuré : renseigne SMTP_USER et SMTP_PASSWORD (mot de passe d'application Gmail, "
                "https://myaccount.google.com/apppasswords) dans .env."
            )
        return user, password

    def _open_smtp(self) -> smtplib.SMTP:
        host, port = self.secrets.smtp_host, int(self.secrets.smtp_port)
        if self._smtp_factory is not None:
            return self._smtp_factory(host, port, timeout=SMTP_TIMEOUT_S)
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=ssl.create_default_context())
        return smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)

    def _smtp_send(self, msg: EmailMessage, to_addr: str) -> None:
        user, password = self._credentials()
        try:
            smtp = self._open_smtp()
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(f"Serveur SMTP injoignable ({self.secrets.smtp_host}) : {exc}") from exc
        try:
            if int(self.secrets.smtp_port) != 465:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(user, password)
            refused = smtp.send_message(msg, from_addr=user, to_addrs=[to_addr])
        except smtplib.SMTPException as exc:
            raise _map_smtp_error(exc, to_addr, self.repo) from exc
        except OSError as exc:
            raise MailError(f"Connexion SMTP interrompue : {exc}") from exc
        finally:
            _quit_quietly(smtp)
        if refused:
            raise MailError(f"Destinataire refusé par le serveur : {to_addr} ({refused.get(to_addr)})")


def _quit_quietly(smtp: smtplib.SMTP) -> None:
    with contextlib.suppress(OSError, smtplib.SMTPException):  # connection already closed by the server
        smtp.quit()


def _map_smtp_error(exc: smtplib.SMTPException, to_addr: str, repo: Repository) -> MailError:
    """Translate Gmail SMTP errors (research §2.1) into French, channel-aware exceptions."""
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if "5.7.9" in text:
            return MailFatalError("Gmail exige un mot de passe d'application (534 5.7.9) : crée-le sur "
                                  "https://myaccount.google.com/apppasswords et mets-le dans SMTP_PASSWORD.")
        if "5.7.14" in text:
            return MailFatalError("Gmail demande une vérification de sécurité (534 5.7.14) : connecte-toi une fois "
                                  "sur gmail.com dans ton navigateur, puis réessaie dans 24 h.")
        return MailFatalError(f"Identifiants SMTP refusés ({code}) : le mot de passe d'application est faux ou a "
                              "été révoqué (changement de mot de passe Google ?).")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        repo.blacklist_add(to_addr, "adresse refusée par le serveur SMTP")
        return MailError(f"Destinataire refusé par le serveur, ajouté à la liste noire : {to_addr}")
    if "5.4.5" in text:
        return MailFatalError("Limite d'envoi quotidienne Gmail atteinte (550 5.4.5) : envois suspendus pour 24 h.")
    if "5.7.1" in text:
        return MailFatalError("Gmail a bloqué le message comme indésirable (550 5.7.1) : envois stoppés, "
                              "vérifie tes textes.")
    if "5.3.4" in text or code == 552:
        return MailError("Message trop lourd pour Gmail (552 5.3.4) : réduis la taille des pièces jointes.")
    return MailError(f"Échec de l'envoi SMTP ({code or type(exc).__name__}) : {text[:200]}")


def build_mailer(ctx: AppContext) -> Mailer | None:
    """Dry-run → .eml in output/outbox; missing credentials (real run) → None + warning."""
    outbox = ctx.output_dir / "outbox"
    if ctx.dry_run:
        return Mailer(ctx.secrets, ctx.repo, ctx.profile, dry_run=True, outbox_dir=outbox)
    if not (ctx.secrets.smtp_user and ctx.secrets.smtp_password):
        log.warning(
            "Envoi d'e-mails désactivé : SMTP_USER / SMTP_PASSWORD absents du .env "
            "(mot de passe d'application Gmail requis)."
        )
        return None
    if ctx.secrets.smtp_user.strip().lower() != ctx.profile.identity.email.strip().lower():
        log.warning("SMTP_USER (%s) diffère de l'e-mail du profil : Gmail peut réécrire l'expéditeur.",
                    ctx.secrets.smtp_user)
    return Mailer(ctx.secrets, ctx.repo, ctx.profile, dry_run=False, outbox_dir=outbox)
