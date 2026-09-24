"""Classroom post → offers (LLM + grounding + fallback) → JobOffer route, school-relayed matching and letter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobbot import clock
from jobbot.apply.board import board_view
from jobbot.classroom.extract import (
    ExtractedOffer,
    PostExtraction,
    build_prompt,
    company_from_email,
    extract_offers,
    fallback_extraction,
    ground,
)
from jobbot.classroom.mapping import board_for, contract_of, to_job
from jobbot.classroom.models import ClassroomPost, LinkContent
from jobbot.llm import FakeLLM
from jobbot.matching import relayed
from jobbot.matching.match import match_job
from jobbot.models import ApplyMethod, ContractType, JobStatus

POST_URL = "https://classroom.google.com/c/MTIzNDU2Nzg5MDEy/p/NTU1MDAwMTExMjIy"
LI_URL = "https://www.linkedin.com/jobs/view/4410135372/"
TEXT = ("Deux offres d'alternance de nos entreprises partenaires.\n"
        "1) Acme Logiciels (Metz) recherche un développeur full-stack React / Node.js / TypeScript en alternance "
        "(Bac+3 à Bac+5, 24 mois). Candidature (CV + lettre) à recrutement@acme-logiciels.fr avant le 30/09/2026.\n"
        "2) Beta Services (Nancy) cherche un technicien support en alternance, BTS SIO. Postuler : " + LI_URL)
OFFERS = {"is_offer_post": True, "offers": [
    {"title": "Développeur full-stack React / Node.js en alternance", "company": "Acme Logiciels",
     "location": "Metz (57)", "contract_type": "alternance", "target_level": "Bac+3 à Bac+5",
     "description": "Développement d'applications web React, Node.js, TypeScript et PostgreSQL. 24 mois.",
     "skills": ["React", "Node.js", "TypeScript"], "contact_email": "recrutement@acme-logiciels.fr",
     "contact_name": None, "apply_url": None, "apply_instructions": "CV + lettre", "apply_via_school": False,
     "deadline": "30/09/2026"},
    {"title": "Technicien support en alternance", "company": "Beta Services", "location": "Nancy (54)",
     "contract_type": "alternance", "target_level": "BTS SIO", "description": "Support utilisateurs.",
     "skills": "Windows, réseau", "contact_email": "", "apply_url": LI_URL, "deadline": ""},
]}


def _post(**updates) -> ClassroomPost:
    base = dict(post_id="p-555000111222", course_id="123456789012", url=POST_URL, author="Mme Claire Exemple",
                date_text="12 sept.", published_at=clock.now(), text=TEXT, links=[LI_URL])
    return ClassroomPost(**{**base, **updates})


def _responder(payload: dict):
    def respond(prompt, system, fast):
        return json.dumps(payload) if "is_offer_post" in prompt else "{}"
    return respond


# --------------------------------------------------------------------------- extraction


def test_extract_with_llm_and_grounding():
    llm = FakeLLM(_responder(OFFERS))
    extraction, used_llm = extract_offers(llm, _post(), [])
    assert used_llm and extraction.is_offer_post and len(extraction.offers) == 2
    first, second = extraction.offers
    assert first.contact_email == "recrutement@acme-logiciels.fr" and first.contact_name == ""
    assert second.skills == ["Windows", "réseau"] and second.apply_url == LI_URL
    prompt, system, fast = llm.calls[0]
    assert fast and "Classroom" in system and "Mme Claire Exemple" in prompt and "is_offer_post" in prompt


def test_grounding_drops_invented_facts():
    invented = PostExtraction(is_offer_post=True, offers=[
        ExtractedOffer(title="Dev", company="Société Imaginaire", contact_email="rh@imaginaire.example",
                       apply_url="https://imaginaire.example/postuler"),
        ExtractedOffer(),  # nothing at all → removed
    ])
    grounded = ground(invented, _post(), [])
    assert len(grounded.offers) == 1
    offer = grounded.offers[0]
    assert (offer.company, offer.contact_email, offer.apply_url) == ("", "", "")
    assert ground(PostExtraction(is_offer_post=False, offers=invented.offers), _post(), []).offers == []
    linked = LinkContent(url="https://jobs.example.org/9", text="Contact : jobs@delta-data.example")
    kept = ground(PostExtraction(is_offer_post=True, offers=[
        ExtractedOffer(title="Dev Python", contact_email="jobs@delta-data.example",
                       apply_url="https://jobs.example.org/9")]), _post(text="Offre Python"), [linked])
    assert kept.offers[0].contact_email == "jobs@delta-data.example" and kept.offers[0].apply_url


def test_non_offer_posts_and_fallback():
    reminder = _post(text="Rappel : réunion d'information jeudi à 14h en salle 12.", links=[])
    assert not extract_offers(FakeLLM(_responder({"is_offer_post": False, "offers": []})), reminder, [])[0].offers
    assert extract_offers(FakeLLM(), _post(text="Merci !"), [])[0] == PostExtraction()  # too short: no LLM call
    assert fallback_extraction(reminder, []) == PostExtraction()
    broken = FakeLLM("pas du json")
    extraction, used_llm = extract_offers(broken, _post(), [])
    assert not used_llm and len(extraction.offers) == 1
    offer = extraction.offers[0]
    assert offer.title.startswith("Deux offres") and offer.contact_email == "recrutement@acme-logiciels.fr"
    assert offer.company == "Acme Logiciels" and offer.apply_url == LI_URL
    assert company_from_email("jean@gmail.com") == "" and company_from_email("x@beta-services.fr") == "Beta Services"


def test_prompt_contains_attachments_and_links():
    from jobbot.classroom.models import Attachment

    post = _post(attachments=[Attachment(title="Fiche Acme", url="https://docs.google.com/document/d/1Ab/edit",
                                         kind="doc", text="Missions : API REST")])
    prompt = build_prompt(post, [LinkContent(url=LI_URL, kind="board", text="Offre LinkedIn : support"),
                                 LinkContent(url="https://x", kind="error", text="HTTP 500")])
    assert "Pièce jointe : Fiche Acme" in prompt and "Missions : API REST" in prompt
    assert "Offre LinkedIn : support" in prompt and "HTTP 500" not in prompt


# --------------------------------------------------------------------------- mapping


def _offers() -> list[ExtractedOffer]:
    return [ExtractedOffer.model_validate(o) for o in OFFERS["offers"]]


def test_mapping_email_route_and_raw(settings):
    job = to_job(settings, _post(), _offers()[0], 1, [], only_offer=False)
    assert job.source == "classroom" and job.external_id == "p-555000111222-1" and job.url == POST_URL
    assert job.apply_method == ApplyMethod.EMAIL and job.contact_email == "recrutement@acme-logiciels.fr"
    assert job.contract_type == ContractType.ALTERNANCE and job.company == "Acme Logiciels"
    raw = job.raw
    assert raw["relayed_by_school"] is True and raw["post_id"] == "p-555000111222" and raw["course_id"]
    assert raw["target_level"] == "Bac+3 à Bac+5" and raw["deadline"] == "30/09/2026"
    assert raw["school_bonus"] == settings.classroom.school_bonus and "board" not in raw
    assert "Date limite : 30/09/2026" in job.description and "Compétences : React" in job.description


def test_mapping_board_routes(settings, make_job):
    offer = _offers()[1]
    easy = make_job(source="linkedin", external_id="4410135372", url=LI_URL, title="Technicien support H/F",
                    company="Beta Services", apply_method=ApplyMethod.LINKEDIN_EASY_APPLY, contact_email=None,
                    description="Description complète LinkedIn " * 40, raw={"easy_apply": True})
    link = LinkContent(url=LI_URL, kind="board", board=easy)
    assert board_for(offer, [link], only_offer=False) is link
    job = to_job(settings, _post(), offer, 2, [link], only_offer=False)
    assert job.apply_method == ApplyMethod.LINKEDIN_EASY_APPLY and job.apply_url == LI_URL
    assert job.title == "Technicien support H/F" and job.description.startswith("Description complète LinkedIn")
    assert job.raw["board"]["source"] == "linkedin" and job.raw["board_source"] == "linkedin"
    view = board_view(job)
    assert (view.source, view.external_id, view.url, view.id) == ("linkedin", "4410135372", LI_URL, job.id)
    assert view.raw["easy_apply"] is True and board_view(view.model_copy(update={"raw": {}})).source == "linkedin"
    external = easy.model_copy(update={"apply_method": ApplyMethod.EXTERNAL, "apply_url": "https://ats.example/1"})
    job = to_job(settings, _post(), offer, 2, [LinkContent(url=LI_URL, kind="board", board=external)],
                 only_offer=False)
    assert job.apply_method == ApplyMethod.EXTERNAL and job.apply_url == "https://ats.example/1"
    assert board_view(job) is job
    expired = easy.model_copy(update={"status": JobStatus.EXPIRED})
    job = to_job(settings, _post(), offer, 2, [LinkContent(url=LI_URL, kind="board", board=expired)],
                 only_offer=False)
    assert job.apply_method == ApplyMethod.EXTERNAL and job.raw["board_expired"] is True


def test_mapping_single_board_link_and_lba(settings, make_job):
    lba_url = "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/offres_emploi_lba/abc123/offre"
    lba = make_job(source="lba", external_id="abc123", url=lba_url, apply_method=ApplyMethod.LBA,
                   contact_email=None, raw={"source": "offres_emploi_lba", "id": "abc123", "recipient_id": "r1"})
    offer = ExtractedOffer(title="Développeur web en alternance", company="Gamma Web")
    job = to_job(settings, _post(text="Gamma Web recrute", links=[lba_url]), offer, 1,
                 [LinkContent(url=lba_url, kind="board", board=lba)], only_offer=True)
    assert job.apply_method == ApplyMethod.LBA
    view = board_view(job)
    assert view.source == "lba" and view.raw["source"] == "offres_emploi_lba" and view.raw["id"] == "abc123"
    plain = to_job(settings, _post(text="Gamma Web recrute", links=[]), offer, 1, [], only_offer=True)
    assert plain.apply_method == ApplyMethod.EXTERNAL and plain.apply_url is None and plain.url == POST_URL


def test_contract_mapping():
    assert contract_of("Contrat de professionnalisation", "Dev", "") == ContractType.ALTERNANCE
    assert contract_of("CDI", "Dev", "") == ContractType.CDI
    assert contract_of("", "Stage développeur", "") == ContractType.STAGE
    assert contract_of("", "Développeur", "en alternance sur 24 mois") == ContractType.ALTERNANCE


# --------------------------------------------------------------------------- matching of relayed offers


def test_relayed_offer_skips_school_filter_and_gets_bonus(settings, profile, make_job):
    school_words = ("Offre relayée par notre école : rejoignez notre campus partenaire. Acme Logiciels recrute un "
                    "développeur React / Node.js / TypeScript en alternance, Bac+5, 24 mois.")
    base = make_job(description=school_words, company="Acme Logiciels")
    score_plain, reasons_plain = match_job(base, profile)
    assert score_plain == 0 and "école" in reasons_plain[0]
    relayed_job = base.model_copy(update={"raw": {"relayed_by_school": True, "school_bonus": 5.0,
                                                  "target_level": "Bac+3 à Bac+5"}})
    score, reasons = match_job(relayed_job, profile)
    assert score > 45 and any("Offre relayée par l'école (+5 points)" == r for r in reasons)
    no_bonus = relayed_job.model_copy(update={"raw": {**relayed_job.raw, "school_bonus": 0}})
    assert score == pytest.approx(min(100.0, match_job(no_bonus, profile)[0] + 5), abs=0.2)  # capped at 100


def test_relayed_level_deadline_and_expired_board(profile, make_job):
    job = make_job(raw={"relayed_by_school": True, "target_level": "BTS SIO / Bac+2"})
    score, reasons = match_job(job, profile)
    assert score == 0 and "BTS SIO / Bac+2" in reasons[0] and "Bac+5" in reasons[0]
    for fine in ("Bac+3", "Master", "Mastère", "ingénieur", "RNCP 7", "Bac+2 à Bac+5", ""):
        assert relayed.level_reason(job.model_copy(update={"raw": {"target_level": fine}}), profile) is None, fine
    assert relayed.target_levels("CAP ou Bac pro") == {3, 4}
    assert relayed.level_reason(job.model_copy(update={"raw": {"target_level": "CAP"}}), profile)
    past = job.model_copy(update={"raw": {"relayed_by_school": True, "deadline": "01/09/2026"}})
    assert "date limite" in relayed.deadline_reason(past, datetime(2026, 9, 24, tzinfo=UTC))
    assert relayed.deadline_reason(past, datetime(2026, 8, 1, tzinfo=UTC)) is None
    assert relayed.parse_deadline("avant le 15 octobre", datetime(2026, 9, 1).date()).month == 10
    assert relayed.parse_deadline("2026-10-02", datetime(2026, 9, 1).date()).day == 2
    assert relayed.parse_deadline("dès que possible", datetime(2026, 9, 1).date()) is None
    expired = job.model_copy(update={"raw": {"relayed_by_school": True, "board_expired": True, "board_source": "hellowork"}})
    assert "hellowork" in match_job(expired, profile)[1][0]
    assert relayed.school_bonus(job.model_copy(update={"raw": {"relayed_by_school": True, "school_bonus": "x"}})) == 5.0
    assert relayed.school_bonus(make_job()) == 0.0


# --------------------------------------------------------------------------- letter & e-mail


def test_letter_mentions_the_school_relay(profile, make_job):
    from jobbot.documents.letter import write_cover_letter
    from jobbot.documents.models import letter_to_text

    job = make_job(raw={"relayed_by_school": True})
    letter = write_cover_letter(profile, FakeLLM("{}"), job=job)
    sentence = "J'ai pris connaissance de votre offre par l'intermédiaire de Exemple Digital School."
    assert letter.paragraphs[0].startswith(sentence) and letter_to_text(letter).count(sentence) == 1
    assert sentence in letter.email_body
    plain = write_cover_letter(profile, FakeLLM("{}"), job=make_job())
    assert "par l'intermédiaire de" not in letter_to_text(plain) + plain.email_body
    no_school = profile.model_copy(update={"alternance": None})
    assert "par l'intermédiaire de mon école" in write_cover_letter(no_school, FakeLLM("{}"), job=job).email_body
