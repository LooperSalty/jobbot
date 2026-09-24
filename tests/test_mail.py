"""Mailer tests: fake SMTP, no network."""

from __future__ import annotations

import dataclasses
import email
import email.policy
import logging
import smtplib
from datetime import timedelta
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.config import Secrets
from jobbot.mail.inbox import _next_status
from jobbot.mail.mailer import (
    SUBJECT_KV_PREFIX,
    Mailer,
    MailError,
    MailFatalError,
    OutgoingEmail,
    ascii_filename,
    build_mailer,
    html_alternative,
    normalize_msgid,
    parse_recipient,
    reply_subject,
)
from jobbot.models import Application, ApplicationStatus, ApplyMethod

APP_PASSWORD = "abcd efgh ijkl mnop"


class FakeSMTP:
    """Records the SMTP dialogue; can be told to fail at a given step."""

    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float = 0, *, fail: dict | None = None, refused=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[tuple] = []
        self.fail = fail or {}
        self.refused = refused or {}
        FakeSMTP.instances.append(self)

    def _maybe_fail(self, step: str) -> None:
        if step in self.fail:
            raise self.fail[step]

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        assert context is not None

    def login(self, user, password):
        self.calls.append("login")
        self.credentials = (user, password)
        self._maybe_fail("login")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append("send")
        self._maybe_fail("send")
        self.sent.append((msg, from_addr, to_addrs))
        return self.refused

    def quit(self):
        self.calls.append("quit")


def factory(**kwargs):
    FakeSMTP.instances = []

    def _make(host, port, timeout=0):
        return FakeSMTP(host, port, timeout, **kwargs)

    return _make


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password=APP_PASSWORD)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "CV_Jean_DUPONT_Développeur (1).pdf"
    path.write_bytes(b"%PDF-1.4 fake cv")
    return path


def _mailer(secrets, repo, profile, **kwargs) -> Mailer:
    return Mailer(secrets, repo, profile, smtp_factory=factory(**kwargs.pop("smtp", {})), **kwargs)


def _parsed(fake: FakeSMTP) -> email.message.EmailMessage:
    msg = fake.sent[0][0]
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Candidature", "Re: Candidature"), ("RE : TR: Fwd: Candidature", "Re: Candidature"),
     ("Re: Re: x\r\nBcc: evil@x.fr", "Re: x Bcc: evil@x.fr")],
)
def test_reply_subject_is_normalised(raw, expected):
    assert reply_subject(raw) == expected


def test_parse_recipient_accepts_one_valid_address():
    assert parse_recipient("Mme Émilie Durand <E.Durand@Example.fr>") == ("Mme Émilie Durand", "e.durand@example.fr")
    for bad in ("not-an-email", "a@b.fr, c@d.fr", "x@y.fr\r\nBcc: evil@z.fr", "", "a..b@x.fr"):
        with pytest.raises(MailError):
            parse_recipient(bad)


def test_ascii_filename_and_msgid_normalisation():
    assert ascii_filename("CV_Jean_DUPONT_Développeur (1).pdf") == "CV_Jean_DUPONT_Developpeur_1.pdf"
    assert ascii_filename("lettre é.PDF") == "lettre_e.pdf"
    assert normalize_msgid("abc@gmail.com") == "<abc@gmail.com>"
    assert normalize_msgid("<a b@c>") is None
    assert normalize_msgid(None) is None


def test_html_alternative_escapes_and_has_no_images():
    out = html_alternative("Bonjour <b>\n\nLigne 1\nLigne 2")
    assert "&lt;b&gt;" in out and "<br>" in out and "<img" not in out


# --------------------------------------------------------------------------- sending


def test_send_builds_threaded_message_over_starttls(secrets, repo, profile, pdf):
    mailer = _mailer(secrets, repo, profile)
    mail = OutgoingEmail(
        to="Mme Durand <rh@acme-logiciels.fr>", subject="RE: Re: Candidature – Développeur", body="Bonjour,\n\nMerci.",
        attachments=[str(pdf)], in_reply_to="<b2@gmail.com>", references=["<a1@gmail.com>", "bad ref"],
        kind="followup", application_id=7,
    )
    sent = mailer.send(mail)
    fake = FakeSMTP.instances[0]
    assert fake.calls[:4] == ["ehlo", "starttls", "ehlo", "login"] and fake.calls[-1] == "quit"
    assert fake.credentials == ("jean.dupont@example.com", "abcdefghijklmnop")
    assert fake.sent[0][2] == ["rh@acme-logiciels.fr"]
    msg = _parsed(fake)
    assert msg["Subject"] == "Re: Candidature – Développeur"
    assert msg["In-Reply-To"] == "<b2@gmail.com>"
    assert msg["References"] == "<a1@gmail.com> <b2@gmail.com>"
    assert msg["Message-ID"].endswith("@example.com>") and "jobbot" in msg["Message-ID"]  # sender's domain
    assert "List-Unsubscribe" not in msg and "Precedence" not in msg
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in types and "text/html" in types and "application/pdf" in types
    assert [p.get_filename() for p in msg.iter_attachments()] == ["CV_Jean_DUPONT_Developpeur_1.pdf"]
    assert sent.message_id == msg["Message-ID"] and sent.kind == "followup" and sent.application_id == 7
    assert not sent.dry_run
    assert repo.count_sent_emails_since(clock.now() - timedelta(minutes=5)) == 1
    assert repo.kv_get(SUBJECT_KV_PREFIX + sent.message_id) == "Re: Candidature – Développeur"


def test_first_email_keeps_subject_and_has_no_thread_headers(secrets, repo, profile):
    mailer = _mailer(secrets, repo, profile)
    mailer.send(OutgoingEmail(to="rh@acme.fr", subject="Candidature\r\nBcc: evil@x.fr", body="Bonjour",
                              kind="application"))
    msg = _parsed(FakeSMTP.instances[0])
    assert msg["Subject"] == "Candidature Bcc: evil@x.fr"
    assert msg["Bcc"] is None and msg["In-Reply-To"] is None and msg["References"] is None
    assert msg["From"] == "Jean DUPONT <jean.dupont@example.com>"


def test_implicit_tls_port_skips_starttls(repo, profile):
    s = Secrets(_env_file=None, smtp_user="jean.dupont@example.com", smtp_password="x" * 16, smtp_port=465)
    _mailer(s, repo, profile).send(OutgoingEmail(to="rh@acme.fr", subject="Candidature", body="B", kind="application"))
    assert "starttls" not in FakeSMTP.instances[0].calls


def test_blacklisted_recipient_or_domain_is_refused(secrets, repo, profile):
    mailer = _mailer(secrets, repo, profile)
    repo.blacklist_add("rh@acme.fr", "opt-out")
    repo.blacklist_add("spam-corp.fr", "domaine")
    for to in ("rh@acme.fr", "jobs@spam-corp.fr"):
        with pytest.raises(MailError, match="liste noire"):
            mailer.send(OutgoingEmail(to=to, subject="Candidature", body="B", kind="application"))
    assert FakeSMTP.instances == []


def test_dry_run_writes_eml_and_does_not_count(secrets, repo, profile, tmp_path):
    outbox = tmp_path / "outbox"
    mailer = Mailer(secrets, repo, profile, dry_run=True, outbox_dir=outbox, smtp_factory=factory())
    sent = mailer.send(OutgoingEmail(to="rh@acme.fr", subject="Candidature", body="Bonjour", kind="spontaneous"))
    files = list(outbox.glob("*.eml"))
    assert len(files) == 1 and "spontaneous" in files[0].name
    parsed = email.message_from_bytes(files[0].read_bytes(), policy=email.policy.default)
    assert parsed["Message-ID"] == sent.message_id
    assert sent.dry_run and FakeSMTP.instances == []
    assert repo.count_sent_emails_since(clock.now() - timedelta(minutes=5)) == 0


def test_missing_attachment_and_big_attachment(secrets, repo, profile, tmp_path, caplog):
    mailer = _mailer(secrets, repo, profile)
    with pytest.raises(MailError, match="introuvable"):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", attachments=[str(tmp_path / "nope.pdf")],
                                  kind="application"))
    big = tmp_path / "big.pdf"
    big.write_bytes(b"0" * (2 * 1024 * 1024 + 10))
    with caplog.at_level(logging.WARNING):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", attachments=[str(big)], kind="application"))
    assert "volumineuses" in caplog.text


def test_missing_credentials_is_fatal(repo, profile):
    mailer = _mailer(Secrets(_env_file=None), repo, profile)
    mailer.sender = "jean.dupont@example.com"
    with pytest.raises(MailFatalError, match="SMTP non configuré"):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", kind="application"))


@pytest.mark.parametrize(
    ("step", "exc", "expected", "fatal"),
    [
        ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"), "révoqué", True),
        ("login", smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required"),
         "mot de passe d'application", True),
        ("login", smtplib.SMTPAuthenticationError(534, b"5.7.14 Please log in via your web browser"), "sécurité", True),
        ("send", smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded"), "Limite", True),
        ("send", smtplib.SMTPDataError(550, b"5.7.1 likely unsolicited mail"), "indésirable", True),
        ("send", smtplib.SMTPDataError(552, b"5.3.4 message too big"), "trop lourd", False),
        ("send", smtplib.SMTPServerDisconnected("gone"), "Échec", False),
        ("send", TimeoutError("timed out"), "interrompue", False),
    ],
)
def test_smtp_errors_are_mapped(secrets, repo, profile, step, exc, expected, fatal):
    mailer = _mailer(secrets, repo, profile, smtp={"fail": {step: exc}})
    with pytest.raises(MailError, match=expected) as info:
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", kind="application"))
    assert isinstance(info.value, MailFatalError) is fatal
    assert FakeSMTP.instances[0].calls[-1] == "quit"
    assert repo.count_sent_emails_since(clock.now() - timedelta(minutes=5)) == 0


def test_refused_recipient_is_blacklisted(secrets, repo, profile):
    refused = smtplib.SMTPRecipientsRefused({"rh@acme.fr": (550, b"5.1.1 unknown")})
    mailer = _mailer(secrets, repo, profile, smtp={"fail": {"send": refused}})
    with pytest.raises(MailError, match="liste noire"):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", kind="application"))
    assert repo.is_blacklisted("rh@acme.fr")


def test_partial_refusal_raises(secrets, repo, profile):
    mailer = _mailer(secrets, repo, profile, smtp={"refused": {"rh@acme.fr": (550, b"no")}})
    with pytest.raises(MailError, match="refusé"):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", kind="application"))


def test_connection_failure(secrets, repo, profile):
    def boom(host, port, timeout=0):
        raise ConnectionRefusedError("refused")

    mailer = Mailer(secrets, repo, profile, smtp_factory=boom)
    with pytest.raises(MailError, match="injoignable"):
        mailer.send(OutgoingEmail(to="rh@acme.fr", subject="C", body="B", kind="application"))


def test_empty_subject_refused(secrets, repo, profile):
    with pytest.raises(MailError, match="Objet"):
        _mailer(secrets, repo, profile).send(OutgoingEmail(to="rh@acme.fr", subject=" \r\n", body="B",
                                                           kind="application"))


# --------------------------------------------------------------------------- inbox status rule


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (ApplicationStatus.REJECTED, ApplicationStatus.RESPONSE, ApplicationStatus.REJECTED),
        (ApplicationStatus.INTERVIEW, ApplicationStatus.RESPONSE, ApplicationStatus.INTERVIEW),
        (ApplicationStatus.NO_RESPONSE, ApplicationStatus.RESPONSE, ApplicationStatus.RESPONSE),
        (ApplicationStatus.REJECTED, ApplicationStatus.INTERVIEW, ApplicationStatus.INTERVIEW),
    ],
)
def test_plain_response_never_overrides_informative_status(current, target, expected):
    app = Application(method=ApplyMethod.EMAIL, applied_at=clock.now(), status=current)
    assert _next_status(app, target) == expected


# --------------------------------------------------------------------------- build_mailer


def test_build_mailer_modes(ctx, caplog):
    dry = build_mailer(ctx)
    assert dry is not None and dry.dry_run and dry.outbox_dir == ctx.output_dir / "outbox"
    real_ctx = dataclasses.replace(ctx, dry_run=False)
    with caplog.at_level(logging.WARNING):
        assert build_mailer(real_ctx) is None
    assert "SMTP_USER" in caplog.text
    creds = Secrets(_env_file=None, smtp_user="other@gmail.com", smtp_password=APP_PASSWORD)
    with caplog.at_level(logging.WARNING):
        mailer = build_mailer(dataclasses.replace(real_ctx, secrets=creds))
    assert mailer is not None and not mailer.dry_run and mailer.sender == "other@gmail.com"
    assert "diffère" in caplog.text
