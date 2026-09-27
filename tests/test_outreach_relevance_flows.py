"""outreach.relevance applied everywhere a spontaneous application can leave: LBA relay (stored data, then the
recruiter detail), e-mail campaign, enrichment, daily summary. Fakes come from test_outreach_spontaneous."""

# ruff: noqa: F811 — pytest fixtures imported from test_outreach_spontaneous are "redefined" as parameters

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import pipeline
from jobbot.apply.lba import APPLY_URL, LbaApplier
from jobbot.models import JobStatus
from jobbot.outreach.relevance import IRRELEVANT_STATUS, REASON_KEY
from jobbot.outreach.spontaneous import CampaignReport, run_spontaneous_campaign

from .fixtures.orchestration.fakes import live, with_settings, write_docs
from .test_outreach_spontaneous import (  # noqa: F401 — fixtures reused
    FakeMailer,
    _company,
    _store,
    docs_calls,
    live_ctx,
    sleeps,
)

SIRET = "85190096900011"
DETAIL_URL = "https://labonnealternance.apprentissage.beta.gouv.fr/api/_private/jobs/recruteurs_lba/{}"
DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "sources_api" / "lba_detail_recruiter.json"


def _detail(**updates) -> dict:
    return {**json.loads(DETAIL_FIXTURE.read_text(encoding="utf-8")), **updates}


def _recruiter(make_company, siret: str = SIRET, **fields):
    raw = {"siret": siret, "hasEmail": True, **fields.pop("raw", {})}
    return make_company(source="lba_recruteur", contacts=[], siren=siret[:9], raw=raw, **fields)


def _public_works(make_company, siret: str = "11122233300011"):
    return _recruiter(make_company, siret, name="TRAVAUX EXEMPLE TP", naf_code="42.11Z", headcount_range="10-19",
                      raw={"outreach_score": 99})


# --------------------------------------------------------------------------- LBA relay


@respx.mock
def test_lba_applier_refuses_off_target_recruiter_before_any_request(ctx, make_company):
    company = _public_works(make_company)
    result = LbaApplier(live(ctx)).apply_to_recruiter(company, write_docs(ctx, company_id=company.id))
    assert result.status == JobStatus.SKIPPED and not result.ok
    assert result.detail == "Entreprise hors cible (secteur hors numérique (NAF 42.11Z, 10-19 salariés)) : rien envoyé."
    assert not respx.calls  # not even the detail: LBA allows 3 applications per company, ever


@respx.mock
def test_lba_relay_sets_aside_off_target_recruiters_without_documents(ctx, make_company, monkeypatch):
    import jobbot.documents.service as docs_service
    from jobbot.apply.lba_recruiters import run_lba_recruiters

    monkeypatch.setattr(docs_service, "prepare_documents", lambda *a, **k: pytest.fail("no LLM time for them"))
    good = _recruiter(make_company)
    ctx.repo.upsert_company(good)
    write_docs(ctx, company_id=good.id)
    off = _public_works(make_company)
    ctx.repo.upsert_company(off)
    respx.get(DETAIL_URL.format(SIRET)).mock(return_value=httpx.Response(200, json=_detail()))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(202, json={"id": "1"}))

    report = run_lba_recruiters(live(ctx))

    assert (report.applied, report.irrelevant, post.call_count) == (1, 1, 1)
    stored = ctx.repo.get_company(off.id)
    assert stored.status == IRRELEVANT_STATUS and "NAF 42.11Z" in stored.raw[REASON_KEY]
    assert report.details[0].startswith("TRAVAUX EXEMPLE TP : hors cible, écartée (secteur hors numérique")
    assert run_lba_recruiters(live(ctx)).irrelevant == 0  # set aside once, never considered again


@respx.mock
def test_lba_relay_detail_reveals_the_real_sector(ctx, make_company):
    from jobbot.apply.lba_recruiters import run_lba_recruiters

    site_hit = _recruiter(make_company, name="VOIRIE DE L'EST", naf_code=None, naf_label="PROGRAMMATION INFORMATIQUE")
    ctx.repo.upsert_company(site_hit)
    write_docs(ctx, company_id=site_hit.id)
    detail = _detail(nafs=[{"code": "42.11Z", "label": "Construction de routes et autoroutes"}])
    detail["company"] = {**detail["company"], "size": "10-19"}
    respx.get(DETAIL_URL.format(SIRET)).mock(return_value=httpx.Response(200, json=detail))
    post = respx.post(APPLY_URL).mock(return_value=httpx.Response(202, json={}))

    report = run_lba_recruiters(live(ctx))

    assert (report.applied, report.irrelevant) == (0, 1) and not post.called
    stored = ctx.repo.get_company(site_hit.id)
    assert stored.status == IRRELEVANT_STATUS and "NAF 42.11Z, 10-19 salariés" in stored.raw[REASON_KEY]


def test_off_target_companies_leave_the_review_queue(ctx, make_company):
    import jobbot.apply.lba_recruiters as relay

    for n in range(relay.REVIEW_QUEUE_MAX):  # a full LBA validation queue of public-works companies
        ctx.repo.upsert_company(_recruiter(make_company, f"{n:09d}00011", name=f"TP {n}", naf_code="42.11Z",
                                           status="pending_review", raw={"review_channel": "lba"}))
    good = _recruiter(make_company)
    ctx.repo.upsert_company(good)
    write_docs(ctx, company_id=good.id)

    report = relay.run_lba_recruiters(with_settings(ctx, mode="review"))

    assert (report.pending_review, report.irrelevant) == (1, relay.REVIEW_QUEUE_MAX)
    assert [c.id for c in ctx.repo.list_companies("pending_review")] == [good.id]


# --------------------------------------------------------------------------- e-mail campaign


def test_campaign_skips_stored_off_target_companies(live_ctx, make_company, docs_calls, sleeps):
    good = _company(make_company, "111111111", "Alpha Web", score=1)
    public_works = _company(make_company, "222222222", "TRAVAUX EXEMPLE TP", score=9).model_copy(
        update={"naf_code": "42.11Z", "headcount_range": "10 à 19 salariés"})
    hvac = _company(make_company, "333333333", "CLIM EXEMPLE", status="approved").model_copy(
        update={"naf_code": "43.22B"})
    _store(live_ctx, good, public_works, hvac)
    mailer = FakeMailer()

    report = run_spontaneous_campaign(live_ctx, mailer)

    assert [e.to for e in mailer.sent] == ["recrutement@111111111.fr"] and docs_calls == ["Alpha Web"]
    assert (report.sent, report.irrelevant) == (1, 2)
    assert {live_ctx.repo.get_company(c.id).status for c in (public_works, hvac)} == {IRRELEVANT_STATUS}
    assert sum("hors cible, écartée" in d for d in report.details) == 2


# --------------------------------------------------------------------------- enrichment & summary


def test_enrichment_sets_aside_off_target_companies(ctx, make_company, monkeypatch):
    from jobbot.outreach import emails

    ctx.repo.upsert_company(make_company(siren="100000001", name="Boite Web"))
    off = make_company(siren="100000002", name="TRAVAUX EXEMPLE TP", naf_code="42.11Z")
    ctx.repo.upsert_company(off)
    seen: list[str] = []

    def fake_enrich(c, company):
        seen.append(company.name)
        return company.model_copy(update={"status": "no_email"})

    monkeypatch.setattr(emails, "enrich_company", fake_enrich)
    report = emails.enrich_companies(ctx)
    assert seen == ["Boite Web"] and (report.processed, report.irrelevant) == (1, 1)
    assert ctx.repo.get_company(off.id).status == IRRELEVANT_STATUS


def test_daily_summary_counts_off_target_companies(ctx, monkeypatch):
    import jobbot.apply.lba_recruiters as relay
    from jobbot.outreach import spontaneous

    monkeypatch.setattr(relay, "run_lba_recruiters",
                        lambda c, limit=None: relay.RelayReport(applied=1, irrelevant=2, details=["X : hors cible"]))
    monkeypatch.setattr(spontaneous, "run_spontaneous_campaign",
                        lambda c, m, limit=None: CampaignReport(sent=1, skipped=0, failed=0, irrelevant=1))
    merged = pipeline.spontaneous_applications(ctx, None)
    assert (merged.sent, merged.irrelevant) == (2, 3) and merged.details == ["LBA — X : hors cible"]
    summary = pipeline.DailyReport(spontaneous=merged).summary()
    assert "2 spontanée(s) (3 entreprise(s) hors cible écartée(s))" in summary
