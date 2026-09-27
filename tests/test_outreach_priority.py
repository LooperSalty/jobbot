"""outreach.priority — 0-100 priority with French reasons, ready queue, campaign order and reuse of fresh documents."""

# ruff: noqa: F811 — pytest fixtures imported from test_outreach_spontaneous are "redefined" as parameters

from __future__ import annotations

from datetime import timedelta

import pytest

from jobbot import clock
from jobbot.config import OutreachSettings
from jobbot.models import Application, ApplyMethod, ContactEmail, TailoredDocuments
from jobbot.outreach.priority import company_priority, potential, ready_queue
from jobbot.outreach.spontaneous import fresh_documents, run_spontaneous_campaign
from tests.test_outreach_spontaneous import (  # noqa: F401 — fixtures reused
    FakeMailer,
    _store,
    docs_calls,
    live_ctx,
    sleeps,
)

RECRUIT = ContactEmail(email="recrutement@exemple-dev.fr", confidence=0.9, kind="recruitment")
CONTACT = ContactEmail(email="contact@exemple-dev.fr", confidence=0.8, kind="generic")
NAMED_HR = ContactEmail(email="claire.dupuis@exemple-dev.fr", confidence=0.7, kind="hr", person_name="Claire DUPUIS")
GUESS = ContactEmail(email="paul.martin@exemple-dev.fr", confidence=0.3, source="pattern", kind="tech",
                     person_name="Paul MARTIN")
METZ = {"latitude": 49.12, "longitude": 6.18}


def _signal(days_ago: int, title: str = "Développeur React (H/F)") -> dict:
    return {"title": title, "source": "lba", "date": clock.iso(clock.now() - timedelta(days=days_ago))}


def _company(make_company, siren: str = "900000001", **overrides):
    base = {"siren": siren, "name": f"Exemple Dev {siren[-2:]}", "status": "enriched", "contacts": [RECRUIT],
            "naf_code": "62.01Z", "domain": f"exemple-{siren}.fr", "raw": {"tranche_effectif_salarie": "11"}, **METZ}
    return make_company(**{**base, **overrides})


def _score(company, profile, repo, **kw):
    return company_priority(company, profile, repo, **kw)


def test_best_case_adds_every_part_and_explains_it(make_company, profile, repo):
    company = _company(make_company, raw={"tranche_effectif_salarie": "11", "hiring_signal": [_signal(10)],
                                          "site_skills": ["React", "TypeScript", "Docker"]})
    score, reasons = _score(company, profile, repo)
    assert score == 100  # 30 + 25 + 20 + 10 + 10 + 6 = 101, capped
    text = " | ".join(reasons)
    for expected in ("cœur de cible (NAF 62.01Z) +30", "Recrute des développeurs", "+25",
                     "Adresse recrutement / RH", "+20", "de Metz +10", "tutorat", "Compétences du profil"):
        assert expected in text


@pytest.mark.parametrize(("contact", "raw", "points"), [
    (RECRUIT, {}, 20), (NAMED_HR, {}, 15), (CONTACT, {}, 0),
    (GUESS, {"email_pattern": "{first}.{last}"}, 5), (GUESS, {}, -10),
])
def test_contact_quality(make_company, profile, repo, contact, raw, points):
    base = _company(make_company, contacts=[RECRUIT], raw={}, latitude=None, longitude=None, naf_code="70.22Z")
    probe = _company(make_company, contacts=[contact], raw=raw, latitude=None, longitude=None, naf_code="70.22Z")
    reference, _ = _score(base, profile, repo)
    score, _ = _score(probe, profile, repo)
    assert score == max(1, reference - 20 + points)


def test_sector_buckets(make_company, profile, repo):
    def sector(**overrides):
        company = _company(make_company, **{"latitude": None, "longitude": None, "raw": {}, **overrides})
        return _score(company, profile, repo)[0] - 20  # minus the recruitment address

    assert sector(naf_code="62.01Z") == 30
    assert sector(naf_code="61.10Z") == 15  # telecoms: digital, not core
    assert sector(naf_code="86.10Z", raw={"tranche_effectif_salarie": "41"}) == 10  # hospital ≥ 500: IT dept likely
    assert sector(naf_code="47.11B") == 0


def test_distance_and_size(make_company, profile, repo):
    far = _company(make_company, latitude=48.95, longitude=6.35, raw={})  # ≈ 23 km from Metz, 31 km from Nancy
    near = _company(make_company, raw={})
    assert _score(near, profile, repo)[0] - _score(far, profile, repo)[0] == 5
    big = _company(make_company, raw={"tranche_effectif_salarie": "32"})  # 250-499: no tutoring bonus
    small = _company(make_company, raw={"tranche_effectif_salarie": "12"})
    assert _score(small, profile, repo)[0] - _score(big, profile, repo)[0] == 10


def test_old_hiring_signal_does_not_count(make_company, profile, repo):
    fresh = _company(make_company, raw={"hiring_signal": [_signal(10)]})
    stale = _company(make_company, raw={"hiring_signal": [_signal(90)]})
    assert _score(fresh, profile, repo)[0] - _score(stale, profile, repo)[0] == 25


def test_exclusions_and_no_address(make_company, profile, repo):
    assert _score(_company(make_company, contacts=[]), profile, repo) == (
        0.0, ["Aucune adresse utilisable : hors file d'attente"])
    assert _score(_company(make_company, status="contacted"), profile, repo)[1] == ["Exclue : déjà contactée"]
    blocked = _company(make_company, siren="900000002")
    repo.blacklist_add(blocked.domain)
    assert _score(blocked, profile, repo)[0] == 0
    applied = _company(make_company, siren="900000003")
    repo.create_application(Application(method=ApplyMethod.LBA, company_name=applied.name, applied_at=clock.now()))
    assert "moins de 60 jours" in _score(applied, profile, repo)[1][0]
    guess_only = _company(make_company, siren="900000004", contacts=[GUESS])
    strict = OutreachSettings(allow_unverified_guesses=False)
    assert _score(guess_only, profile, repo, cfg=strict)[0] == 0


def test_potential_ignores_the_contact(make_company, profile):
    company = _company(make_company, contacts=[], raw={"hiring_signal": [_signal(3)]})
    assert potential(company, profile) == 30 + 25 + 10


def test_ready_queue_validated_first_then_priority(ctx, make_company):
    low = _company(make_company, siren="900000011", contacts=[CONTACT], latitude=None, longitude=None, raw={})
    high = _company(make_company, siren="900000012", raw={"hiring_signal": [_signal(2)]})
    approved = _company(make_company, siren="900000013", status="approved", contacts=[CONTACT], raw={})
    no_mail = _company(make_company, siren="900000014", contacts=[])
    _store(ctx, low, high, approved, no_mail)
    order = [r.company.id for r in ready_queue(ctx)]
    assert order == [approved.id, high.id, low.id]


# --------------------------------------------------------------------------- campaign


def test_campaign_picks_targets_by_priority(live_ctx, make_company, docs_calls, sleeps):
    plain = _company(make_company, siren="900000021", name="Sans Signal", contacts=[CONTACT],
                     raw={"outreach_score": 9})
    hiring = _company(make_company, siren="900000022", name="Recrute", contacts=[CONTACT],
                      raw={"outreach_score": 1, "hiring_signal": [_signal(5)]})
    hiring = hiring.model_copy(update={"contacts": [CONTACT.model_copy(update={"email": "contact@recrute.fr"})]})
    _store(live_ctx, plain, hiring)
    mailer = FakeMailer()
    run_spontaneous_campaign(live_ctx, mailer)
    assert docs_calls == ["Recrute", "Sans Signal"]  # hiring signal (+25) beats the discovery score


def _docs(ctx, company, tmp_path, *, days_ago: int) -> TailoredDocuments:
    cv = tmp_path / f"cv-{company.id}.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    docs = TailoredDocuments(company_id=company.id, cv_pdf=str(cv), letter_text="Lettre préparée la nuit",
                             email_subject="Candidature", email_body="Madame, Monsieur,\n\nMa candidature.",
                             created_at=clock.now() - timedelta(days=days_ago))
    ctx.repo.save_documents(docs)
    return docs


def test_campaign_reuses_documents_prepared_the_night_before(live_ctx, make_company, docs_calls, sleeps, tmp_path):
    ready = _company(make_company, siren="900000031", name="Dossier Pret")
    stale = _company(make_company, siren="900000032", name="Dossier Ancien", contacts=[CONTACT])
    _store(live_ctx, ready, stale)
    _docs(live_ctx, ready, tmp_path, days_ago=0)
    _docs(live_ctx, stale, tmp_path, days_ago=9)
    assert fresh_documents(live_ctx.repo, ready.id) is not None
    assert fresh_documents(live_ctx.repo, stale.id) is None
    mailer = FakeMailer()
    report = run_spontaneous_campaign(live_ctx, mailer)
    assert report.sent == 2
    assert docs_calls == ["Dossier Ancien"]  # the fresh night documents were reused
    assert mailer.sent[0].subject == "Candidature"


def test_fresh_documents_need_the_cv_file(ctx, make_company, tmp_path):
    company = _company(make_company)
    docs = _docs(ctx, company, tmp_path, days_ago=1)
    (tmp_path / f"cv-{company.id}.pdf").unlink()
    assert docs.company_id == company.id
    assert fresh_documents(ctx.repo, company.id) is None
