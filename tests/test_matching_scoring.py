"""matching.match.score_new_jobs: batch scoring against the real Repository (no network)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.matching import match as match_mod
from jobbot.matching.match import MatchReport, score_new_jobs
from jobbot.models import JobOffer, JobStatus
from jobbot.textutil import stable_id

FIXTURES = Path(__file__).parent / "fixtures" / "matching"
OFFERS = json.loads((FIXTURES / "offers.json").read_text(encoding="utf-8"))


def offer(key: str) -> JobOffer:
    data = OFFERS[key]
    return JobOffer(id=stable_id(data["source"], data["external_id"]), found_at=clock.now(),
                    published_at=clock.now() - timedelta(days=1), **data)


@pytest.fixture
def geocode_calls(monkeypatch):
    calls: list[str] = []

    def fake_geocode(query, client=None):
        calls.append(query)
        return (49.2353, 6.1719) if "Talange" in query else None

    monkeypatch.setattr(match_mod, "geocode", fake_geocode)
    return calls


def test_score_new_jobs_statuses_duplicates_and_geocoding(ctx, make_job, geocode_calls):
    repo = ctx.repo
    good, school = offer("fullstack_alternance"), offer("school_iscod")
    handled = make_job(source="hellowork", external_id="hw-1", company="Doublon SA", title="Développeur React (H/F)")
    duplicate = make_job(source="linkedin", external_id="li-1", company="Doublon SA", title="Développeur React (H/F)")
    talange = make_job(external_id="t-1", company="Talange Tech", location="Talange, Grand Est, France",
                       postal_code=None, latitude=None, longitude=None)
    anonymous = [make_job(external_id=f"anon-{i}", company="", title="Développeur web (H/F)") for i in range(2)]
    for job in (good, school, handled, duplicate, talange, *anonymous):
        repo.upsert_job(job)
    repo.set_job_match(handled.id, 80.0, ["déjà retenue"], JobStatus.SHORTLISTED)

    report = score_new_jobs(ctx)

    assert report == MatchReport(scored=6, shortlisted=4, filtered_out=1)
    assert repo.get_job(good.id).status == JobStatus.SHORTLISTED
    assert repo.get_job(good.id).match_score >= 85
    stored_school = repo.get_job(school.id)
    assert stored_school.status == JobStatus.FILTERED_OUT and stored_school.match_score == 0
    assert "ISCOD" in stored_school.match_reasons[0]
    dup = repo.get_job(duplicate.id)
    assert dup.status == JobStatus.SKIPPED and dup.match_reasons[0].startswith("Doublon")
    assert repo.get_job(handled.id).status == JobStatus.SHORTLISTED  # untouched (not NEW)
    located = repo.get_job(talange.id)
    assert (located.latitude, located.longitude) == (49.2353, 6.1719)
    assert any("km de Metz" in r for r in located.match_reasons)
    assert geocode_calls == ["Talange"]
    assert all(repo.get_job(j.id).status == JobStatus.SHORTLISTED for j in anonymous)  # no company → no dedup


def test_score_new_jobs_isolates_a_failing_job(ctx, make_job, geocode_calls, monkeypatch):
    ok, bad = make_job(external_id="ok"), make_job(external_id="bad", company="Autre")
    for job in (ok, bad):
        ctx.repo.upsert_job(job)
    real = match_mod._match

    def flaky(job, profile, skills):
        if job.id == bad.id:
            raise RuntimeError("boom")
        return real(job, profile, skills)

    monkeypatch.setattr(match_mod, "_match", flaky)
    report = score_new_jobs(ctx)
    assert report.scored == 1 and report.shortlisted == 1
    assert ctx.repo.get_job(bad.id).status == JobStatus.NEW


def test_score_new_jobs_with_nothing_to_do(ctx):
    assert score_new_jobs(ctx) == MatchReport()


def test_locate_keeps_job_when_geocoding_fails_or_saving_fails(ctx, make_job, monkeypatch):
    job = make_job(location="Hameau Inconnu", postal_code=None, latitude=None, longitude=None)
    monkeypatch.setattr(match_mod, "geocode", lambda q, client=None: None)
    assert match_mod._locate(ctx, job) is job
    monkeypatch.setattr(match_mod, "geocode", lambda q, client=None: (48.0, 6.0))

    def broken_save(_job):
        raise OSError("disque plein")

    monkeypatch.setattr(ctx.repo, "save_job", broken_save)
    located = match_mod._locate(ctx, job)
    assert (located.latitude, located.longitude) == (48.0, 6.0)
    remote = make_job(location="Télétravail", postal_code=None, latitude=None, longitude=None)
    assert match_mod._locate(ctx, remote) is remote


def test_locate_never_geocodes_a_foreign_place(ctx, make_job, geocode_calls):
    job = make_job(location="Belval, Luxembourg", postal_code=None, latitude=None, longitude=None)
    assert match_mod._locate(ctx, job) is job
    assert geocode_calls == []


def test_contract_restricted_area_only_counts_for_its_contracts(profile, make_job):
    from jobbot.models import ContractType

    lux = dict(location="Luxembourg", postal_code=None, latitude=None, longitude=None)
    cdi = make_job(external_id="lu-cdi", contract_type=ContractType.CDI, title="Développeur full-stack (H/F) - CDI",
                   **lux)
    alternance = make_job(external_id="lu-alt", **lux)  # Luxembourg is a CDI-only area in the profile
    assert match_mod.match_job(cdi, profile)[0] > 45
    score, reasons = match_mod.match_job(alternance, profile)
    assert score == 0
    assert "offre en alternance au Luxembourg" in reasons[0]


def test_locate_never_geocodes_an_offer_detected_abroad(ctx, make_job, geocode_calls):
    job = make_job(location="Arlon, BE", postal_code=None, latitude=None, longitude=None)
    assert match_mod._locate(ctx, job) is job and geocode_calls == []
