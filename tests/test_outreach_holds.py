"""outreach.holds: companies held « à valider » by review mode only go back to the sending queue in auto mode."""

from __future__ import annotations

from datetime import date

from jobbot import pipeline
from jobbot.apply import lba_recruiters
from jobbot.outreach.holds import release_review_holds
from jobbot.sources.lba_mapping import RECRUITER_SOURCE

from .fixtures.orchestration.fakes import FakeMailer, live, with_settings, write_docs

HELD_RAW = {"review_contact": "recrutement@acme-logiciels.fr", "review_cv": "cv.pdf", "outreach_score": 50}


def _held(ctx, make_company, siren: str, *, warnings: list[str] | None = None, **fields):
    raw = {**HELD_RAW, **fields.pop("raw", {})}
    company = make_company(siren=siren, name=f"Entreprise {siren}", status="pending_review", raw=raw, **fields)
    ctx.repo.upsert_company(company)
    docs = write_docs(ctx, company_id=company.id)
    if warnings:
        ctx.repo.save_documents(docs.model_copy(update={"truth_warnings": warnings}))
    return company


def test_auto_mode_releases_what_review_mode_held(ctx, make_company):
    run = live(ctx)
    email = _held(run, make_company, "111111111")
    lba = _held(run, make_company, "222222222", source=RECRUITER_SOURCE, contacts=[],
                raw={"review_contact": "via La Bonne Alternance", "review_channel": "lba", "hasEmail": True,
                     "siret": "22222222200011"})
    retry = _held(run, make_company, "444444444", raw={"review_prev_status": "contacted"})
    claims = _held(run, make_company, "333333333", warnings=["Compétence absente du profil : Rust"])

    assert release_review_holds(run) == 3

    assert run.repo.get_company(email.id).status == "enriched"  # an address to write to
    assert run.repo.get_company(lba.id).status == "new"  # no address: the LBA relay only
    assert lba.id in {c.id for c in lba_recruiters.candidates(run)}
    assert run.repo.get_company(retry.id).status == "contacted"  # a retry after a bounce, as before the hold
    for company in (email, lba, retry):
        raw = run.repo.get_company(company.id).raw
        assert not {"review_contact", "review_cv", "review_channel", "review_prev_status"} & set(raw)
        assert raw["outreach_score"] == 50
    assert run.repo.get_company(claims.id).status == "pending_review"  # unbacked CV claims: the user decides


def test_review_mode_warm_up_and_dry_run_keep_the_holds(ctx, make_company):
    held = _held(ctx, make_company, "111111111")
    assert release_review_holds(with_settings(live(ctx), mode="review")) == 0
    warm = with_settings(live(ctx), setup_date=date.today().isoformat(), warmup_days=3)
    assert release_review_holds(warm) == 0
    assert release_review_holds(ctx) == 0  # simulation: nothing changes
    assert ctx.repo.get_company(held.id).status == "pending_review"


def test_a_decision_taken_meanwhile_in_the_dashboard_wins(ctx, make_company, monkeypatch):
    run = live(ctx)
    held = _held(run, make_company, "111111111")
    snapshot = run.repo.list_companies("pending_review")
    run.repo.upsert_company(held.model_copy(update={"status": "rejected"}))  # « Écarter » clicked meanwhile
    monkeypatch.setattr(run.repo, "list_companies", lambda *a, **k: snapshot)
    assert release_review_holds(run) == 0
    assert run.repo.get_company(held.id).status == "rejected"


def test_spontaneous_step_releases_the_holds_first(ctx, make_company, monkeypatch):
    run = live(ctx)
    held = _held(run, make_company, "111111111")
    seen: list[str] = []

    def campaign(c, mailer, limit=None):
        seen.extend(company.status for company in c.repo.list_companies() if company.id == held.id)
        return pipeline.CampaignReport(sent=0, skipped=0, failed=0)

    monkeypatch.setattr("jobbot.outreach.spontaneous.run_spontaneous_campaign", campaign)
    monkeypatch.setattr("jobbot.apply.lba_recruiters.run_lba_recruiters",
                        lambda c, limit=None: pipeline.ApplyReport())
    report = pipeline.spontaneous_applications(run, FakeMailer())
    assert seen == ["enriched"]
    assert any("remise(s) dans la file" in line for line in report.details)
