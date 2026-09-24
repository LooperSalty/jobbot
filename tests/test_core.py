from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from jobbot import clock
from jobbot.config import ConfigError, Limits, load_profile
from jobbot.llm import FakeLLM, LLMError, extract_json
from jobbot.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    ApplyMethod,
    ATSReport,
    JobStatus,
    SentEmail,
    TailoredDocuments,
)
from jobbot.pacing import Pacer, QuotaExceeded
from jobbot.textutil import dedup_key, extract_emails, normalize, normalize_company, slugify


def test_profile_loads_and_exposes_truth_base(profile):
    assert profile.identity.full_name == "Jean DUPONT"
    skills = profile.all_skills()
    assert "TypeScript" in skills and "PostgreSQL" in skills
    assert "ACME INDUSTRIE" in profile.full_text()


def test_invalid_profile_raises_config_error(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("identity: {first_name: A}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_profile(bad)
    with pytest.raises(ConfigError, match="jobbot init"):
        load_profile(tmp_path / "absent.yaml")


def test_normalize_keeps_tech_tokens():
    assert normalize("Développeur C# / Node.js — CI/CD") == "developpeur c# / node.js ci/cd"
    assert normalize_company("ACME Logiciels SAS") == "acme logiciels"


def test_dedup_key_ignores_gender_suffix():
    assert dedup_key("Acme SAS", "Développeur Web (H/F)") == dedup_key("ACME", "Développeur web")


def test_extract_emails_and_slug():
    assert extract_emails("Contact: RH@Acme.fr, rh@acme.fr.") == ["rh@acme.fr"]
    assert slugify("Développeur Full-Stack (H/F)") == "developpeur-full-stack-h-f"


def test_job_upsert_is_idempotent_and_keeps_status(repo, make_job):
    job = make_job()
    assert repo.upsert_job(job) is True
    repo.set_job_match(job.id, 72.5, ["stack ok"], JobStatus.SHORTLISTED)
    longer = job.model_copy(update={"description": job.description + " Plus de détails.", "status": JobStatus.NEW})
    assert repo.upsert_job(longer) is False
    stored = repo.get_job(job.id)
    assert stored.status == JobStatus.SHORTLISTED
    assert stored.match_score == 72.5
    assert stored.description.endswith("Plus de détails.")
    assert repo.list_jobs(JobStatus.SHORTLISTED)[0].id == job.id


def test_job_upsert_merges_raw_and_keeps_known_values(repo, make_job):
    detailed = make_job(source="lba", external_id="x1", raw={"source": "offres_emploi_lba", "recipient_id": "r-1"})
    repo.upsert_job(detailed)
    card = make_job(source="lba", external_id="x1", company="", location="", description="",
                    raw={"source": "offres_emploi_lba", "recipient_id": None, "application_count": 3})
    assert repo.upsert_job(card) is False
    stored = repo.get_job(detailed.id)
    assert stored.company == detailed.company
    assert stored.location == detailed.location
    assert stored.description == detailed.description
    assert stored.raw == {"source": "offres_emploi_lba", "recipient_id": "r-1", "application_count": 3}
    alert = make_job(source="lba", external_id="x1", title="Titre d'alerte",
                     raw={"from_alert": True, "needs_details": True, "application_count": 0})
    repo.upsert_job(alert)
    stored = repo.get_job(detailed.id)
    assert stored.raw["application_count"] == 3
    assert stored.raw["from_alert"] is True
    assert stored.title == detailed.title  # an alert never overwrites what a source already gave
    renamed = make_job(source="lba", external_id="x1", title="Développeuse web", raw={})
    repo.upsert_job(renamed)
    assert repo.find_duplicate_jobs(make_job(source="x", external_id="other", title="Développeuse web"))


def test_secrets_ignore_blank_and_comment_values(tmp_path, monkeypatch):
    from jobbot.config import load_secrets

    for var in ("HUNTER_API_KEY", "BRAVE_API_KEY", "SMTP_USER"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "HUNTER_API_KEY=          # hunter.io — trouver des e-mails\nBRAVE_API_KEY=\nSMTP_USER=moi@gmail.com\n",
        encoding="utf-8",
    )
    secrets = load_secrets(tmp_path)
    assert secrets.hunter_api_key is None
    assert secrets.brave_api_key is None
    assert secrets.smtp_user == "moi@gmail.com"


def test_request_with_retry_retries_419(monkeypatch):
    import httpx
    import respx

    from jobbot.http import make_client, request_with_retry

    monkeypatch.setattr("jobbot.http.time.sleep", lambda _s: None)
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.example.org/x").mock(side_effect=[httpx.Response(419), httpx.Response(200)])
        with make_client() as client:
            assert request_with_retry(client, "GET", "https://api.example.org/x").status_code == 200


def test_duplicate_detection_across_sources(repo, make_job):
    a = make_job(source="hellowork", external_id="h1", title="Développeur Web (H/F)")
    b = make_job(source="linkedin", external_id="l1", title="Développeur web")
    repo.upsert_job(a)
    repo.upsert_job(b)
    assert [j.id for j in repo.find_duplicate_jobs(b)] == [a.id]


def test_company_roundtrip(repo, make_company):
    c = make_company()
    assert repo.upsert_company(c) is True
    got = repo.get_company(c.id)
    assert got.contacts[0].email == "recrutement@acme-logiciels.fr"
    assert repo.upsert_company(got.model_copy(update={"status": "contacted"})) is False
    assert repo.list_companies("contacted")[0].id == c.id


def test_documents_roundtrip(repo):
    report = ATSReport(score=81, keyword_score=80, title_score=90, section_score=100, format_score=100)
    docs = TailoredDocuments(job_id="j1", cv_pdf="C:/x/cv.pdf", cv_ats=report, created_at=clock.now())
    repo.save_documents(docs)
    got = repo.latest_documents(job_id="j1")
    assert got.cv_ats.score == 81


def test_application_lifecycle(repo):
    now = clock.now()
    app = repo.create_application(
        Application(
            method=ApplyMethod.EMAIL, applied_at=now, company_name="Acme SAS", job_title="Dev",
            email_to="rh@acme.fr", email_message_id="<m1@x>", next_followup_at=now - timedelta(minutes=1),
        )
    )
    assert app.id is not None
    assert repo.recently_applied_to_company("ACME") is True
    assert [a.id for a in repo.due_followups()] == [app.id]
    followed = app.model_copy(
        update={"status": ApplicationStatus.FOLLOWED_UP, "email_thread_ids": ["<m1@x>", "<m2@x>"], "followup_count": 1}
    )
    repo.update_application(followed)
    assert repo.find_application_by_message_id("<m2@x>").id == app.id
    repo.add_event(ApplicationEvent(application_id=app.id, at=now, kind="followup_sent"))
    assert repo.list_events(app.id)[0].kind == "followup_sent"


def test_sent_email_counting_ignores_dry_run(repo):
    now = clock.now()
    repo.log_sent_email(SentEmail(to="a@b.fr", subject="s", message_id="<1@x>", kind="spontaneous", sent_at=now))
    repo.log_sent_email(
        SentEmail(to="a@b.fr", subject="s", message_id="<2@x>", kind="spontaneous", sent_at=now, dry_run=True)
    )
    assert repo.count_sent_emails_since(now - timedelta(hours=1), "spontaneous") == 1


def test_blacklist_and_kv(repo):
    repo.blacklist_add("Stop@Acme.fr", "opt-out")
    assert repo.is_blacklisted("stop@acme.fr", "other")
    assert not repo.is_blacklisted("")
    repo.kv_set("k", "v")
    assert repo.kv_get("k") == "v"


def test_pacer_quota(repo):
    slept: list[float] = []
    pacer = Pacer(repo, Limits(linkedin_per_day=1, min_delay_s=1, max_delay_s=2), sleep=slept.append)
    pacer.ensure_available("linkedin")
    pacer.record("linkedin", ok=False)
    assert pacer.remaining("linkedin") == 1  # failures don't consume quota
    pacer.record("linkedin", ok=True)
    with pytest.raises(QuotaExceeded):
        pacer.ensure_available("linkedin")
    assert 1 <= pacer.between_applications() <= 2 and slept


class _Out(BaseModel):
    ok: bool
    n: int


def test_llm_json_extraction_and_repair():
    assert extract_json('Voici:\n```json\n{"ok": true, "n": 3}\n```') == '{"ok": true, "n": 3}'
    llm = FakeLLM(["pas du json", '{"ok": true, "n": 2}'])
    assert llm.complete_json("q", _Out).n == 2
    assert len(llm.calls) == 2
    with pytest.raises(LLMError):
        FakeLLM(["nope", "still nope"]).complete_json("q", _Out)


def test_context_builds_from_project_root(ctx):
    assert ctx.profile.identity.first_name == "Jean"
    assert ctx.settings.mode in {"auto", "review"}
    assert ctx.output_dir.exists()


def test_find_cli_falls_back_to_native_installer_folder(tmp_path, monkeypatch):
    import sys

    from jobbot.llm import find_cli

    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr("jobbot.llm.Path.home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "vide"))
    assert find_cli("claude") is not None
    assert find_cli("introuvable-xyz") is None
