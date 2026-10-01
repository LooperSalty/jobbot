"""Inbox tests: fake IMAP server, .eml fixtures, FakeLLM. No network."""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.config import Secrets
from jobbot.llm import FakeLLM
from jobbot.mail import inbox as inbox_mod
from jobbot.mail.classify import classify_reply, parse_return_date
from jobbot.mail.imap_client import FolderCursor, quote_mailbox, special_folders
from jobbot.mail.inbox import fetch_new_messages, load_cursors, process_inbox
from jobbot.mail.mailer import MailError
from jobbot.mail.messages import InboundMessage
from jobbot.mail.parsing import parse_message, strip_quoted
from jobbot.mail.platforms import detect_platform_notice
from jobbot.models import Application, ApplicationStatus, ApplyMethod

FIX = Path(__file__).parent / "fixtures" / "mail"
FIRST_ID = "<179016435516.26396.1.jobbot@gmail.com>"


def _load_fakes():
    spec = importlib.util.spec_from_file_location("jobbot_tests_imap_fakes", FIX / "imap_fakes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fakes = _load_fakes()
ME, ALL, SPAM = _fakes.ME, _fakes.ALL, _fakes.SPAM
FakeIMAP, gmail, make_raw = _fakes.FakeIMAP, _fakes.gmail, _fakes.make_raw


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None, smtp_user=ME, smtp_password="abcd efgh ijkl mnop")


@pytest.fixture
def mail_ctx(ctx, secrets):
    return dataclasses.replace(ctx, secrets=secrets, llm=FakeLLM("{}"))


@pytest.fixture
def notified(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(inbox_mod, "notify", lambda ctx, title, message, level="info": calls.append((title, message)))
    return calls


@pytest.fixture
def no_alerts(monkeypatch):
    fake = types.ModuleType("jobbot.sources.alerts")
    fake.jobs_from_alert_email = lambda *a: []
    monkeypatch.setitem(sys.modules, "jobbot.sources.alerts", fake)
    return fake


def make_app(repo, **overrides) -> Application:
    base = dict(
        method=ApplyMethod.EMAIL, applied_at=clock.now() - timedelta(days=3), company_name="Acme Logiciels",
        job_title="Développeur Full-Stack", email_to="rh@acme-logiciels.fr", email_message_id=FIRST_ID,
        email_thread_ids=[FIRST_ID], next_followup_at=clock.now() + timedelta(days=4),
    )
    base.update(overrides)
    return repo.create_application(Application(**base))


# --------------------------------------------------------------------------- parsing


def test_parse_human_reply_decodes_rfc2047_and_multipart():
    msg = parse_message(fixture("human_reply_interview.eml"), uid=5, folder=ALL, gm_thread_id="42")
    assert msg.from_addr == "amelie.durand@acme-logiciels.fr" and msg.from_name == "Amélie Durand"
    assert msg.subject == "RE : Candidature – Développeur Full-Stack"
    assert msg.in_reply_to == FIRST_ID and msg.references == [FIRST_ID]
    assert msg.date == datetime(2026, 9, 24, 8, 42, tzinfo=UTC)
    assert "visio mardi 29 septembre à 10h" in msg.body_text and "<p>" in msg.body_html
    assert not msg.auto_submitted and not msg.is_bounce and msg.gm_thread_id == "42"
    new = strip_quoted(msg.body_text)
    assert "entretien" in new and "Malheureusement" not in new and "a écrit" not in new


def test_parse_hard_bounce():
    msg = parse_message(fixture("gmail_dsn.eml"), uid=1, folder=ALL)
    assert msg.is_bounce and msg.bounced_recipient == "rh@acme-logiciels.fr"
    assert FIRST_ID in msg.references  # original Message-ID recovered from the attached message
    assert "Malheureusement" not in msg.body_text  # the attached original is not part of the body


def test_parse_soft_bounce_and_oof():
    soft = parse_message(fixture("soft_bounce.eml"), uid=2, folder=ALL)
    assert not soft.is_bounce and soft.auto_submitted and soft.bounced_recipient == "rh@slow-server.fr"
    oof = parse_message(fixture("exchange_oof.eml"), uid=3, folder=ALL)
    assert oof.auto_submitted and oof.subject.startswith("Réponse automatique")


def test_parse_html_only_and_plain_bounce_without_dsn():
    lba = parse_message(fixture("lba_refus.eml"), uid=4, folder=ALL)
    assert "ne donne malheureusement pas suite" in lba.body_text and lba.body_html
    raw = make_raw(frm="MAILER-DAEMON <mailer-daemon@example.org>", subject="Undelivered Mail Returned to Sender",
                   body="Delivery to the following recipient failed permanently:\n\n    ghost@nowhere.fr\n")
    bounce = parse_message(raw, uid=9, folder=ALL)
    assert bounce.is_bounce and bounce.bounced_recipient == "ghost@nowhere.fr"


# --------------------------------------------------------------------------- classification


def _msg(raw: bytes) -> InboundMessage:
    return parse_message(raw, uid=1, folder=ALL)


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (fixture("gmail_dsn.eml"), "bounce"),
        (fixture("exchange_oof.eml"), "auto_reply"),
        (fixture("human_reply_interview.eml"), "interview"),
        (fixture("soft_bounce.eml"), "auto_reply"),
        (make_raw(body="Bonjour, merci de ne plus me contacter. Cordialement"), "unsubscribe"),
        (make_raw(body="Non merci."), "unsubscribe"),
        (make_raw(body="Bonjour, nous ne pouvons pas donner une suite favorable à votre candidature."), "rejection"),
        (make_raw(body="Malheureusement, le poste a été pourvu."), "rejection"),
        (make_raw(body="Unfortunately we have decided to move forward with other candidates for this position."),
         "rejection"),
        (make_raw(body="Could you share your availability for a call next week?"), "interview"),
        (make_raw(body="Nous avons bien reçu votre candidature. Si votre profil correspond, nous vous proposerons "
                       "un entretien.", headers={"Auto-Submitted": "auto-generated"}), "auto_reply"),
        (make_raw(body="Newsletter", headers={"List-Unsubscribe": "<mailto:u@x.fr>"}), "auto_reply"),
        (make_raw(body="Pourriez-vous me transmettre votre calendrier d'alternance ?"), "info_request"),
        (make_raw(body="Votre profil nous intéresse beaucoup."), "positive"),
        (make_raw(body="Pouvons-nous prévoir un entretien ?"), "interview"),
        (make_raw(body="Je suis actuellement en congés, de retour le 12/10."), "auto_reply"),
    ],
)
def test_classify_rules(raw, category):
    result = classify_reply(_msg(raw), None)
    assert result.category == category
    assert 0 < result.confidence <= 1 and result.summary


def test_classify_uses_llm_fallback_then_other():
    msg = _msg(make_raw(body="Merci, je transmets à l'équipe technique."))
    llm = FakeLLM('{"category": "positive", "summary": "Transmis à l\'équipe", "confidence": 1.7, "x": 1}')
    result = classify_reply(msg, llm)
    assert result.category == "positive" and result.confidence == 1.0 and result.summary == "Transmis à l'équipe"
    assert llm.calls and llm.calls[0][2] is True  # fast model
    assert classify_reply(msg, FakeLLM("pas du json")).category == "other"
    assert classify_reply(msg, None).confidence == 0.3


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Je suis absent jusqu'au 5 octobre inclus.", datetime(2026, 10, 5).date()),
     ("De retour le lundi 12/10/2026.", datetime(2026, 10, 12).date()),
     ("I am out of office until 3 january", datetime(2027, 1, 3).date()),
     ("Absent jusqu'au 31/02", None), ("Rien à signaler", None)],
)
def test_parse_return_date(text, expected):
    assert parse_return_date(text, datetime(2026, 9, 23, tzinfo=UTC)) == expected


def test_platform_notices():
    refus = detect_platform_notice(_msg(fixture("lba_refus.eml")))
    assert refus.platform == "lba" and refus.company == "Acme Logiciels" and refus.status == ApplicationStatus.REJECTED
    suite = detect_platform_notice(_msg(make_raw(frm="LBA <nepasrepondre@apprentissage.beta.gouv.fr>",
                                                 subject="Suite donnée à votre candidature - Acme Logiciels")))
    assert suite.status == ApplicationStatus.RESPONSE and suite.company == "Acme Logiciels"  # no recruiter text
    viewed = detect_platform_notice(_msg(make_raw(frm="HelloWork <notification@hellowork.com>",
                                                  subject="Votre candidature a été consultée par Acme Logiciels")))
    assert viewed.platform == "hellowork" and viewed.status is None and viewed.company == "Acme Logiciels"
    li = detect_platform_notice(_msg(make_raw(frm="LinkedIn <jobs-noreply@linkedin.com>",
                                              subject="Votre candidature chez Acme Logiciels",
                                              body="Malheureusement, Acme Logiciels a décidé de ne pas donner suite.")))
    assert li.status == ApplicationStatus.REJECTED
    assert detect_platform_notice(_msg(make_raw(frm="LinkedIn <jobs-noreply@linkedin.com>",
                                                subject="12 nouvelles offres pour développeur"))) is None
    assert detect_platform_notice(_msg(make_raw())) is None


LBA_ACK = (
    "Nous avons bien reçu votre candidature et vous en remercions.\n\nVotre candidature a retenu notre attention.\n\n"
    "Nous examinons l'ensemble des candidatures reçues, et reviendrons vers vous prochainement.\n\n"
    "Sans réponse de notre part d’ici 30 jours, veuillez considérer que votre candidature n’a pas été retenue."
)


def _lba_reply(recruiter_text: str) -> bytes:
    """« Suite donnée » e-mail of La Bonne Alternance: its template around the recruiter's own words."""
    body = ("Bonjour Jean DUPONT,\n\nLe recruteur ACME LOGICIELS a bien reçu votre candidature et vous répond :\n\n"
            f"Bonjour,\n\n{recruiter_text}\n\nCordialement,\n\nÀ vous maintenant !\n\n"
            "✅ Lisez attentivement leur message.\n✅ Suivez les instructions proposées par le recruteur pour "
            "poursuivre le processus de candidature.\n✅ Préparez-vous pour un éventuel entretien.\n")
    return make_raw(frm="La bonne alternance <nepasrepondre@apprentissage.beta.gouv.fr>",
                    subject="Suite donnée à votre candidature - ACME LOGICIELS", body=body)


@pytest.mark.parametrize(("recruiter_text", "status"), [
    (LBA_ACK, ApplicationStatus.RESPONSE),  # acknowledgement, « retenu notre attention »: no interview, no refusal
    ("Votre profil nous intéresse. Seriez-vous disponible mardi 6 octobre à 14h pour un entretien en visio ?",
     ApplicationStatus.INTERVIEW),
    ("Nous sommes au regret de ne pas pouvoir donner suite à votre candidature.", ApplicationStatus.REJECTED),
    ("Pourriez-vous nous transmettre votre calendrier d'alternance ?", ApplicationStatus.RESPONSE),
])
def test_lba_reply_status_comes_from_the_recruiter_words(recruiter_text, status):
    notice = detect_platform_notice(_msg(_lba_reply(recruiter_text)))
    assert notice.platform == "lba" and notice.company == "ACME LOGICIELS"
    assert notice.status == status


def test_lba_template_alone_never_announces_an_interview():
    """« Préparez-vous pour un éventuel entretien » is LBA's footer in every reply, not an invitation."""
    notice = detect_platform_notice(_msg(_lba_reply("Merci pour votre candidature.")))
    assert notice.status == ApplicationStatus.RESPONSE
    assert "entretien" not in notice.detail.lower()


def test_a_conditional_refusal_is_no_refusal():
    msg = _msg(make_raw(subject="Re: Candidature", body=LBA_ACK))
    assert classify_reply(msg, None).category != "rejection"


# --------------------------------------------------------------------------- IMAP


def test_special_folders_resolves_localised_names_and_literals():
    imap = gmail({})
    folders = special_folders(imap)
    assert folders["\\All"] == ALL and folders["\\Junk"] == SPAM
    assert quote_mailbox(ALL) == '"[Gmail]/Tous les messages"'


def test_fetch_new_messages_filters_uids_and_skips_own(secrets):
    own = make_raw(frm=f"Jean DUPONT <{ME}>", subject="Candidature", msgid=FIRST_ID)
    imap = gmail({10: (own, "900"), 11: (fixture("human_reply_interview.eml"), "900")},
                 {3: (make_raw(subject="Old spam"), "76"), 12: (make_raw(subject="Spam reply"), "77")})
    msgs = fetch_new_messages(secrets, 9, imap_factory=lambda host, port: imap)
    assert {(m.folder, m.uid) for m in msgs} == {(ALL, 11), (SPAM, 12)}
    assert imap.logged_out
    body_fetches = [c for c in imap.commands if c[0] == "FETCH" and "BODY.PEEK[]" in c[2]]
    assert all("10" not in c[1].split(",") for c in body_fetches)  # our own copy: headers only
    # "UID 12:*" returns the last message anyway: it must be filtered out
    again = fetch_new_messages(secrets, 11, imap_factory=lambda host, port: gmail({11: (own, "1")}))
    assert [m.uid for m in again if m.folder == ALL] == []


def test_fetch_first_run_uses_date_search_and_limit(secrets):
    imap = gmail({u: (make_raw(subject=f"m{u}"), "1") for u in range(1, 6)})
    msgs = fetch_new_messages(secrets, 0, imap_factory=lambda h, p: imap, limit=2)
    assert [m.uid for m in msgs] == [1, 2]
    assert any(c[:2] == ("SEARCH", "SINCE") for c in imap.commands)


def test_fetch_login_failure_raises_mail_error(secrets):
    imap = gmail({}, password_ok=False)
    with pytest.raises(MailError, match="IMAP refusée"):
        fetch_new_messages(secrets, 0, imap_factory=lambda h, p: imap)
    assert imap.logged_out
    with pytest.raises(MailError, match="non configuré"):
        fetch_new_messages(Secrets(_env_file=None), 0, imap_factory=lambda h, p: imap)


def test_fetch_connection_failure(secrets):
    def boom(host, port):
        raise OSError("network down")

    with pytest.raises(MailError, match="injoignable"):
        fetch_new_messages(secrets, 0, imap_factory=boom)


def test_non_gmail_server_falls_back_to_inbox(secrets):
    imap = FakeIMAP({"INBOX": {"flag": "\\Marked", "uidvalidity": 1, "messages": {1: (make_raw(), "0")}}},
                    gmail=False)
    msgs = fetch_new_messages(secrets, 0, imap_factory=lambda h, p: imap)
    assert [m.folder for m in msgs] == ["INBOX"]
    assert all("X-GM-THRID" not in c[2] for c in imap.commands if c[0] == "FETCH")


# --------------------------------------------------------------------------- process_inbox


def test_process_inbox_interview_reply(mail_ctx, notified, no_alerts):
    app = make_app(mail_ctx.repo)
    imap = gmail({50: (fixture("human_reply_interview.eml"), "900")})
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: imap)
    assert (report.scanned, report.matched) == (1, 1)
    stored = mail_ctx.repo.get_application(app.id)
    assert stored.status == ApplicationStatus.INTERVIEW
    assert stored.next_followup_at is None and stored.last_response_at is not None
    assert [e.kind for e in mail_ctx.repo.list_events(app.id)] == ["interview"]
    assert notified and "Acme Logiciels" in notified[0][0]
    assert mail_ctx.repo.kv_get("imap:all:last_uid") == "50" and mail_ctx.repo.kv_get("imap_last_uid") == "50"
    assert mail_ctx.repo.kv_get("imap:all:uidvalidity") == "11"
    # second run: nothing new ("UID 51:*" returns uid 50 again, filtered), no duplicate event
    report2 = process_inbox(mail_ctx, imap_factory=lambda h, p: imap)
    assert report2.scanned == 0 and len(mail_ctx.repo.list_events(app.id)) == 1


def test_process_inbox_bounce_blacklists(mail_ctx, notified, no_alerts):
    app = make_app(mail_ctx.repo, email_thread_ids=[])
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({7: (fixture("gmail_dsn.eml"), "1")}))
    assert report.matched == 1
    stored = mail_ctx.repo.get_application(app.id)
    assert stored.status == ApplicationStatus.BOUNCED and stored.last_response_at is None
    assert mail_ctx.repo.is_blacklisted("rh@acme-logiciels.fr")
    assert notified == []


def test_process_inbox_unsubscribe_and_oof(mail_ctx, notified, no_alerts):
    app = make_app(mail_ctx.repo)
    other = make_app(mail_ctx.repo, company_name="Beta Corp", email_to="jobs@beta-corp.fr",
                     email_message_id="<b1@gmail.com>", email_thread_ids=["<b1@gmail.com>"],
                     next_followup_at=datetime(2026, 9, 30, 7, tzinfo=UTC))
    optout = make_raw(frm="Boss <boss@acme-logiciels.fr>", body="Merci de ne plus me contacter.",
                      in_reply_to=FIRST_ID)
    oof = fixture("exchange_oof.eml").replace(FIRST_ID.encode(), b"<b1@gmail.com>")
    process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({1: (optout, "1"), 2: (oof, "2")}))
    stored = mail_ctx.repo.get_application(app.id)
    assert stored.status == ApplicationStatus.REJECTED
    assert mail_ctx.repo.is_blacklisted("boss@acme-logiciels.fr")
    assert mail_ctx.repo.is_blacklisted("rh@acme-logiciels.fr")
    postponed = mail_ctx.repo.get_application(other.id)
    assert postponed.status == ApplicationStatus.APPLIED
    assert clock.to_paris(postponed.next_followup_at).date() == datetime(2026, 10, 7).date()
    assert notified == []


def test_process_inbox_domain_fallback_and_gmail_thread(mail_ctx, notified, no_alerts):
    app = make_app(mail_ctx.repo)
    own = make_raw(frm=f"Jean DUPONT <{ME}>", subject="Candidature", msgid=FIRST_ID)
    same_thread = make_raw(frm="Recruteur <recruteur@cabinet-rh.fr>", body="Merci, je vous rappelle demain.")
    by_domain = make_raw(frm="Marc <marc@acme-logiciels.fr>", body="Bien noté, on regarde.",
                         date=datetime(2026, 9, 25, tzinfo=UTC))
    imap = gmail({1: (own, "555"), 2: (same_thread, "555"), 3: (by_domain, "777")})
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: imap)
    assert report.matched == 2 and report.scanned == 2
    events = [e.kind for e in mail_ctx.repo.list_events(app.id)]
    assert "interview" in events  # "je vous rappelle" via the Gmail thread id
    assert "note" in events  # unclassified domain match: event only, no status change
    assert mail_ctx.repo.get_application(app.id).status == ApplicationStatus.INTERVIEW


def test_process_inbox_platform_notice_and_spam_folder(mail_ctx, notified, no_alerts):
    app = make_app(mail_ctx.repo, method=ApplyMethod.LBA, email_to=None, email_message_id=None, email_thread_ids=[])
    suite = make_raw(frm="LBA <nepasrepondre@apprentissage.beta.gouv.fr>",
                     subject="Suite donnée à votre candidature - Acme Logiciels",
                     body="Le recruteur ACME LOGICIELS vous répond : Seriez-vous disponible jeudi pour un entretien en "
                          "visio ? À vous maintenant ! Préparez-vous pour un éventuel entretien.")
    unknown = make_raw(frm="LBA <nepasrepondre@apprentissage.beta.gouv.fr>",
                       subject="Réponse négative de Zeta SAS à la candidature de Jean DUPONT")
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({}, {4: (suite, "1"), 5: (unknown, "2")}))
    assert report.matched == 1
    assert mail_ctx.repo.get_application(app.id).status == ApplicationStatus.INTERVIEW
    assert any("spams" in u for u in report.updates)
    assert any("Zeta SAS" in u and "sans candidature" in u for u in report.updates)
    assert notified


def test_process_inbox_job_alerts(mail_ctx, notified, make_job, monkeypatch):
    fake = types.ModuleType("jobbot.sources.alerts")
    calls = []

    def jobs_from_alert_email(from_addr, subject, body_html, body_text):
        calls.append(from_addr)
        if "hellowork" not in from_addr:
            return []
        return [make_job(source="hellowork", external_id="1", company="Nice Co"),
                make_job(source="hellowork", external_id="2", company="Evil Corp")]

    fake.jobs_from_alert_email = jobs_from_alert_email
    monkeypatch.setitem(sys.modules, "jobbot.sources.alerts", fake)
    mail_ctx.repo.blacklist_add("Evil Corp", "test")
    alert = make_raw(frm="HelloWork <alertes@hellowork.com>", subject="3 nouvelles offres", html="<a>x</a>")
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({1: (alert, "1")}))
    assert calls == ["alertes@hellowork.com"]
    assert len(mail_ctx.repo.list_jobs()) == 1
    assert any("2 offre(s), dont 1 nouvelle(s)" in u for u in report.updates)


def test_process_inbox_uidvalidity_change_rescans(mail_ctx, notified, no_alerts):
    make_app(mail_ctx.repo)
    mail_ctx.repo.kv_set("imap:all:uidvalidity", "3")
    mail_ctx.repo.kv_set("imap:all:last_uid", "999")
    imap = gmail({5: (fixture("human_reply_interview.eml"), "1")})
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: imap)
    assert report.matched == 1
    assert imap.commands[0][:2] == ("SEARCH", "SINCE")
    assert load_cursors(mail_ctx.repo)["\\All"] == FolderCursor(11, 5)


def test_process_inbox_errors_never_raise(mail_ctx, ctx, notified, no_alerts, monkeypatch):
    missing = process_inbox(ctx)
    assert missing.scanned == 0 and "SMTP_USER" in missing.updates[0]
    refused = process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({}, password_ok=False))
    assert refused.scanned == 0 and "IMAP" in refused.updates[0]
    make_app(mail_ctx.repo)
    real = inbox_mod.classify_reply

    def flaky(msg, llm):
        if "boom" in msg.body_text:
            raise RuntimeError("boom")
        return real(msg, llm)

    monkeypatch.setattr(inbox_mod, "classify_reply", flaky)
    bad = make_raw(body="boom", in_reply_to=FIRST_ID)
    good = make_raw(frm="Other <o@acme-logiciels.fr>", body="Seriez-vous disponible jeudi ?", in_reply_to=FIRST_ID,
                    date=datetime(2026, 9, 26, tzinfo=UTC))
    report = process_inbox(mail_ctx, imap_factory=lambda h, p: gmail({1: (bad, "1"), 2: (good, "2")}))
    assert report.scanned == 2 and report.matched == 1
    assert any("Erreur" in u for u in report.updates)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("JOBBOT_LIVE_MAIL") != "1", reason="JOBBOT_LIVE_MAIL=1 requis (connexion réelle)")
def test_live_imap_login_and_folders():  # pragma: no cover - needs real credentials, never sends anything
    from jobbot.config import load_secrets
    from jobbot.context import find_root
    from jobbot.mail.imap_client import close_quietly, open_imap

    imap = open_imap(load_secrets(find_root()))
    try:
        assert "\\All" in special_folders(imap)
    finally:
        close_quietly(imap)
