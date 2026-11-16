from jobbot.mail.messages import InboundMessage
from jobbot.mail.redirect import follow_redirect
from jobbot.models import Application, ApplyMethod
from jobbot import clock


def test_recruiter_can_redirect_to_same_company_address(ctx, monkeypatch):
    app = Application(id=4, method=ApplyMethod.EMAIL, applied_at=clock.now(), email_to="contact@acme.lu")
    msg = InboundMessage(uid=1, folder="INBOX", message_id="1", from_addr="contact@acme.lu",
                         body_text="Veuillez envoyer votre candidature à recrutement@acme.lu")
    monkeypatch.setattr("jobbot.mail.redirect._send_to_address", lambda ctx, app, address: address)
    assert follow_redirect(ctx, app, msg, strong=True) == "recrutement@acme.lu"
    assert follow_redirect(ctx, app, msg, strong=False) == ""


def test_external_address_is_flagged_instead_of_sent(ctx, monkeypatch):
    app = Application(id=4, method=ApplyMethod.EMAIL, applied_at=clock.now(), email_to="contact@acme.lu")
    msg = InboundMessage(uid=1, folder="INBOX", message_id="1", from_addr="contact@acme.lu",
                         body_text="Veuillez envoyer votre candidature à jobs@autre.lu")
    notices = []
    monkeypatch.setattr("jobbot.notify.notify", lambda *a, **k: notices.append(a))
    assert "à vérifier" in follow_redirect(ctx, app, msg, strong=True)
    assert len(notices) == 1
