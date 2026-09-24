"""Short descriptions (search cards): capped skill credit, full offer read before scoring (capped per run) and
re-scoring before documents. No network: ``registry.ensure_details`` is replaced by fakes."""

from __future__ import annotations

import pytest

from jobbot import clock, pipeline
from jobbot.config import SearchSettings, Settings, Thresholds
from jobbot.matching import match as match_mod
from jobbot.matching.match import THIN_REASON, match_job
from jobbot.models import ContractType, JobOffer, JobStatus
from jobbot.sources import registry

from .fixtures.orchestration.fakes import store_job, with_settings, write_docs

NEUTRAL = ("Au sein d'une équipe à taille humaine, tu participeras à la vie du produit, des ateliers avec les "
           "utilisateurs jusqu'à la mise à disposition des nouvelles fonctionnalités. Nous privilégions la "
           "bienveillance, l'entraide et le partage de connaissances entre collègues. Les locaux sont faciles d'accès "
           "et un restaurant d'entreprise est à disposition. Le processus de recrutement comprend un échange avec "
           "l'équipe puis un entretien avec la direction.")
FULL = NEUTRAL + " Stack : React, TypeScript, Node.js, Next.js, PostgreSQL, Docker, Git. Débutants acceptés."
NO_PLACE = {"postal_code": None, "latitude": None, "longitude": None}


def card(make_job, external_id: str, title: str = "Développeur Full-Stack React / Node.js (H/F)", **overrides):
    """A LinkedIn guest search card in Metz: title only, contract unknown."""
    base = dict(source="linkedin", external_id=external_id, company=f"Société {external_id}", title=title,
                location="Metz, Grand Est, France", description="", contract_type=ContractType.OTHER,
                published_at=clock.now(), **NO_PLACE)
    return make_job(**{**base, **overrides})


def capped(ctx, cap: int):
    return with_settings(ctx, search=ctx.settings.search.model_copy(update={"max_detail_fetches_per_run": cap}))


# --------------------------------------------------------------------------- thin descriptions


def test_thin_description_caps_the_skill_credit(profile, make_job):
    thin = card(make_job, "4400000001", title="Développeur(se) Fullstack Nextjs")
    full = card(make_job, "4400000001", title="Développeur(se) Fullstack Nextjs", description=NEUTRAL)
    thin_score, thin_reasons = match_job(thin, profile)
    full_score, full_reasons = match_job(full, profile)
    assert "Compétences en commun (1/1) : Next.js" in thin_reasons  # same overlap…
    assert THIN_REASON in thin_reasons and THIN_REASON not in full_reasons
    assert full_score - thin_score == pytest.approx(30 * (1 - 1 / 3), abs=0.2)  # …but 1 skill of 3 expected


def test_short_description_naming_enough_skills_keeps_full_credit(profile, make_job):
    short = card(make_job, "4400000002", description="Stack : React, TypeScript, Node.js, PostgreSQL.")
    padded = card(make_job, "4400000002", description="Stack : React, TypeScript, Node.js, PostgreSQL. " + NEUTRAL)
    short_score, reasons = match_job(short, profile)
    assert THIN_REASON in reasons
    assert short_score == match_job(padded, profile)[0]


def test_thin_threshold_matches_the_details_threshold(make_job):
    assert match_mod.THIN_DESCRIPTION_CHARS == registry.DETAILS_MIN_CHARS
    assert match_mod.is_thin(make_job(description="x" * 399))
    assert not match_mod.is_thin(make_job(description="x" * 400))
    assert SearchSettings().max_detail_fetches_per_run == 60
    assert Settings().search.max_detail_fetches_per_run == 60


# --------------------------------------------------------------------------- score step: details first


@pytest.fixture
def fetched(monkeypatch):
    """Fake ``ensure_details``: completes (and stores) the offer, checking it is still unscored."""
    calls: list[str] = []

    def ensure(c, job: JobOffer) -> JobOffer:
        stored = c.repo.get_job(job.id)
        assert stored.status == JobStatus.NEW and stored.match_score is None  # read before scoring
        calls.append(job.id)
        full = job.model_copy(update={"description": FULL})
        c.repo.save_job(full)
        return full

    monkeypatch.setattr(registry, "ensure_details", ensure)
    monkeypatch.setattr(match_mod, "geocode", lambda query, client=None: None)
    monkeypatch.setattr("jobbot.notify.notify", lambda *a, **k: None)
    return calls


def test_score_step_reads_short_cards_before_scoring_within_the_cap(ctx, make_job, fetched):
    best = [card(make_job, f"44100000{i}") for i in range(2)]
    weak = card(make_job, "4410000099", title="Technicien support informatique (H/F)")  # least promising
    complete = card(make_job, "4410000050", description=FULL)  # already complete: nothing to read
    indeed = card(make_job, "in-1", source="indeed")  # JobSpy returns the full text: nothing to read
    for job in (*best, weak, complete, indeed):
        ctx.repo.upsert_job(job)

    report = pipeline.run_daily(capped(ctx, 2), steps=["score"]).score

    assert sorted(fetched) == sorted(job.id for job in best)
    assert report.completed == 2 and report.scored == 5
    for job in best:
        stored = ctx.repo.get_job(job.id)
        assert stored.status == JobStatus.SHORTLISTED and THIN_REASON not in stored.match_reasons
    assert THIN_REASON in ctx.repo.get_job(weak.id).match_reasons  # beyond the cap: provisional score
    assert THIN_REASON not in ctx.repo.get_job(complete.id).match_reasons


def test_score_step_with_no_budget_or_expired_offers(ctx, make_job, fetched, monkeypatch):
    job = card(make_job, "4420000001")
    ctx.repo.upsert_job(job)
    assert pipeline.score_new_offers(capped(ctx, 0)).completed == 0 and fetched == []
    gone = card(make_job, "4420000002")
    ctx.repo.upsert_job(gone)

    def expired(c, j):
        dead = j.model_copy(update={"status": JobStatus.EXPIRED})
        c.repo.save_job(dead)
        return dead

    monkeypatch.setattr(registry, "ensure_details", expired)
    report = pipeline.score_new_offers(ctx)
    assert report.completed == 1 and report.scored == 0
    assert ctx.repo.get_job(gone.id).status == JobStatus.EXPIRED  # never scored


def test_details_failure_never_blocks_the_scoring(ctx, make_job, fetched, monkeypatch):
    ctx.repo.upsert_job(card(make_job, "4430000001"))

    def boom(c):
        raise RuntimeError("LinkedIn en panne")

    monkeypatch.setattr(registry, "complete_new_offers", boom)
    report = pipeline.score_new_offers(ctx)
    assert report.scored == 1 and report.completed == 0


# --------------------------------------------------------------------------- prepare: score again first


@pytest.fixture
def documents(monkeypatch):
    import jobbot.documents.service as docs_service

    prepared: list[str] = []

    def prepare(c, job=None, company=None):
        prepared.append(job.id)
        c.repo.update_job_status(job.id, JobStatus.DOCS_READY)
        return write_docs(c, job.id)

    monkeypatch.setattr(docs_service, "prepare_documents", prepare)
    return prepared


def test_prepare_rescores_a_stale_shortlisted_offer_and_drops_it(ctx, make_job, documents, monkeypatch):
    stale = store_job(ctx, card(make_job, "4500000001", title="Développeur Front React Audiovisuel H/F",
                                location="Île-de-France, France", description=NEUTRAL),
                      status=JobStatus.SHORTLISTED, score=85, docs=False)  # scored by the old rules
    good = store_job(ctx, card(make_job, "4500000002", description=FULL), status=JobStatus.SHORTLISTED, score=60,
                     docs=False)
    monkeypatch.setattr(registry, "ensure_details", lambda c, job: job)  # nothing new to read

    report = pipeline.prepare_shortlisted(ctx)

    assert documents == [good.id] and (report.prepared, report.skipped) == (1, 1)
    dropped = ctx.repo.get_job(stale.id)
    assert dropped.status == JobStatus.FILTERED_OUT and dropped.match_score == 0
    assert dropped.match_reasons == ["Écartée : trop loin : Île-de-France (hors de tes zones)"]
    assert "écartée à la nouvelle notation (Écartée : trop loin : Île-de-France" in report.details[0]
    assert ctx.repo.get_job(good.id).match_score != 60  # kept, with its fresh score


def test_prepare_drops_an_offer_whose_full_page_places_it_far(ctx, make_job, documents, monkeypatch):
    nextjs = store_job(ctx, card(make_job, "4500000003", title="Développeur(se) Fullstack Nextjs", location="France"),
                       status=JobStatus.SHORTLISTED, score=85, docs=False)

    def details(c, job):  # the LinkedIn page names the city
        return job.model_copy(update={"description": FULL, "location": "Paris 9e, Île-de-France, France"})

    monkeypatch.setattr(registry, "ensure_details", details)
    report = pipeline.prepare_shortlisted(ctx)
    assert documents == [] and report.skipped == 1
    assert "après lecture de l'annonce complète (Écartée : trop loin : Paris 9e" in report.details[0]
    assert ctx.repo.get_job(nextjs.id).status == JobStatus.FILTERED_OUT


def test_prepare_drops_an_offer_now_below_the_threshold(ctx, make_job, documents, monkeypatch):
    strict = with_settings(ctx, thresholds=Thresholds(min_match_score=95))
    job = store_job(ctx, card(make_job, "4500000004", description=FULL), status=JobStatus.SHORTLISTED, score=96,
                    docs=False)
    monkeypatch.setattr(registry, "ensure_details", lambda c, j: j)
    report = pipeline.prepare_shortlisted(strict)
    stored = ctx.repo.get_job(job.id)
    assert documents == [] and stored.status == JobStatus.FILTERED_OUT and 0 < stored.match_score < 95
    assert f"score {stored.match_score:.0f} < 95" in report.details[0]
