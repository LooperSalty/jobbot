"""New sources wired into jobbot: registry, settings, secrets, wizard, doctor, JobSpy hint, shared helpers."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobbot import preferences
from jobbot.config import LocationPref, Secrets, Settings, load_secrets
from jobbot.models import ContractType, JobStatus
from jobbot.sources import ats_fetch, jobspy_source, registry, zones
from jobbot.sources.base import SearchQuery
from jobbot.sources.company_ats import CompanyATSSource
from jobbot.sources.offer_utils import (
    contract_from_hint,
    country_code,
    detect_contract,
    format_salary,
    is_dev_title,
    is_not_dev_title,
    is_senior_title,
    parse_date,
    secret_value,
    with_contract_word,
)

FIX = Path(__file__).parent / "fixtures" / "sources_new"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
METZ = LocationPref(city="Metz", postal_code="57000", radius_km=40, latitude=49.1193, longitude=6.1757)
LUX = LocationPref(city="Luxembourg", radius_km=25, country="LU")
NEW = ("adzuna", "jooble", "company_ats")
KEYS = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_API_KEY")


@pytest.fixture(autouse=True)
def no_keys_in_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def q(**kw) -> SearchQuery:
    base = dict(keywords="développeur", location=METZ, contract_types=[], max_age_days=30)
    base.update(kw)
    return SearchQuery(**base)


# ----------------------------------------------------------------------------- configuration


def test_defaults_settings_file_and_secrets(settings):
    assert Settings().search.sources[-3:] == list(NEW) and Settings().search.max_ats_boards_per_run == 80
    assert all(name in settings.search.sources for name in NEW)  # config/settings.yaml
    assert settings.search.max_ats_boards_per_run == 80
    creds = Secrets(_env_file=None, adzuna_app_id="i", adzuna_app_key="k", jooble_api_key="j")
    assert (creds.adzuna_app_id, creds.adzuna_app_key, creds.jooble_api_key) == ("i", "k", "j")
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert all(f"\n{key}=\n" in example for key in KEYS)
    assert "developer.adzuna.com" in example and "jooble.org/api/about" in example
    assert secret_value("  # à remplir") == "" and secret_value(" abc ") == "abc" and secret_value(None) == ""


def test_setup_wizard_asks_for_the_keys(project_root):
    questions = {q.key: q for q in preferences.SETUP_QUESTIONS}
    for key in KEYS:
        assert questions[key].kind == "secret" and questions[key].section == preferences.SEC_ACCOUNTS
    assert "developer.adzuna.com" in questions["ADZUNA_APP_ID"].help
    assert "jooble.org/api/about" in questions["JOOBLE_API_KEY"].help
    preferences.apply_answer(project_root, questions["ADZUNA_APP_ID"], "id-42")
    preferences.apply_answer(project_root, questions["JOOBLE_API_KEY"], "cle-jooble")
    assert preferences.current_answer(project_root, questions["ADZUNA_APP_ID"]) is True
    assert preferences.current_answer(project_root, questions["ADZUNA_APP_KEY"]) is False
    secrets = load_secrets(project_root)
    assert secrets.adzuna_app_id == "id-42" and secrets.jooble_api_key == "cle-jooble"


# ----------------------------------------------------------------------------- registry & doctor


def test_registry_builds_the_new_sources(ctx):
    sources, problems = registry._build(ctx, ["adzuna", "jooble", "ats", "company_ats"])
    assert [s.name for s in sources] == ["company_ats"]  # alias "ats" → company_ats, built once
    assert sources[0]._repo is ctx.repo and sources[0]._max_boards == ctx.settings.search.max_ats_boards_per_run
    assert "ADZUNA_APP_ID" in problems["adzuna"] and "JOOBLE_API_KEY" in problems["jooble"]
    keyed = dataclasses.replace(ctx, secrets=Secrets(_env_file=None, adzuna_app_id="i", adzuna_app_key="k",
                                                     jooble_api_key="j"))
    assert [s.name for s in registry.build_sources(keyed, list(NEW))] == list(NEW)
    assert {"adzuna", "company_ats"} <= registry.FOREIGN_LOCATION_SOURCES
    assert "jooble" not in registry.FOREIGN_LOCATION_SOURCES and "company_ats" in registry.DETAIL_SOURCES
    queries = [q(keywords=f"développeur {i}") for i in range(30)] + [q(location=LUX)]
    assert len(registry._budgeted("adzuna", queries)) == registry.SOURCE_QUERY_BUDGET["adzuna"]
    assert all(query.location is not LUX for query in registry._budgeted("jooble", queries))


def test_doctor_lists_the_new_sources(ctx):
    from jobbot import cli_admin

    checks = {c.name: c for c in cli_admin.check_sources(ctx)}
    assert checks["Source adzuna"].level == "warn" and "ADZUNA_APP_ID" in checks["Source adzuna"].detail
    assert checks["Source jooble"].level == "warn" and "JOOBLE_API_KEY" in checks["Source jooble"].detail
    assert checks["Source company_ats"].level == "ok"


@respx.mock
def test_collect_then_complete_company_ats_offer(ctx, make_company, monkeypatch):
    monkeypatch.setattr(ats_fetch, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("jobbot.http.time.sleep", lambda s: None)
    ctx.repo.upsert_company(make_company(siren="100000010", name="Metz Services Groupe",
                                         careers_url="https://careers.smartrecruiters.com/MetzServicesGroupe"))
    listing = json.loads((FIX / "smartrecruiters_postings.json").read_text(encoding="utf-8"))
    detail = json.loads((FIX / "smartrecruiters_detail.json").read_text(encoding="utf-8"))
    base = "https://api.smartrecruiters.com/v1/companies/MetzServicesGroupe/postings"
    respx.get(f"{base}/744000100000001").mock(return_value=httpx.Response(200, json=detail))
    respx.get(url__startswith=base).mock(return_value=httpx.Response(200, json=listing))
    src = CompanyATSSource(ctx.repo, profile=ctx.profile, client=httpx.Client(), geocoder=lambda p: None,
                           wttj_source=object())
    report = registry.collect_jobs(ctx, [src], [q(contract_types=[ContractType.ALTERNANCE])])
    assert report.new == 1 and report.per_source == {"company_ats": 1} and not report.errors
    (stored,) = ctx.repo.list_jobs(JobStatus.NEW, source="company_ats")
    assert registry.needs_details(stored)
    done = registry.ensure_details(ctx, stored)
    assert "Spring Boot" in done.description and ctx.repo.get_job(stored.id).description == done.description


# ----------------------------------------------------------------------------- Indeed (JobSpy) on Linux


def test_jobspy_hint_and_linux_interpreter(tmp_path):
    linux = jobspy_source.install_hint("posix")
    command = "uv venv .venv-jobspy --python 3.12 && uv pip install --python .venv-jobspy/bin/python python-jobspy"
    assert command in linux
    assert ".venv-jobspy\\Scripts\\python.exe" in jobspy_source.install_hint("nt")
    assert "python-jobspy" in jobspy_source._INSTALL_HINT
    python = tmp_path / ".venv-jobspy" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    assert jobspy_source._default_python(tmp_path) == str(python)
    (tmp_path / "other").mkdir()
    assert jobspy_source._default_python(tmp_path / "other") == jobspy_source.sys.executable


# ----------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(("title", "description", "hint", "expected"), [
    ("Développeur web (alternance)", "", "fulltime_permanent", ContractType.ALTERNANCE),
    ("Développeur web", "", "apprenticeship", ContractType.ALTERNANCE),
    ("Développeur web", "", "fulltime_fixed_term", ContractType.CDD),
    ("Développeur web", "", "Permanent", ContractType.CDI),
    ("Développeur web", "", "INTERN", ContractType.STAGE),
    ("Développeur web", "Contrat d'apprentissage de 24 mois", "Full-time", ContractType.ALTERNANCE),
    ("Développeur web", "", "Full-time", ContractType.OTHER),
])
def test_detect_contract(title, description, hint, expected):
    assert detect_contract(title, description, hint) == expected


def test_title_filters_country_dates_salary_keywords():
    assert is_dev_title("Développeur Full-Stack") and is_dev_title("Software Engineer") and is_dev_title("DevOps")
    assert not is_dev_title("Account Executive") and not is_dev_title("Sales Engineer")
    assert not is_dev_title("Chargé de développement commercial") and is_not_dev_title("Business Developer")
    assert is_senior_title("Senior Backend Engineer") and is_senior_title("Lead Dev") and not is_senior_title("Dev")
    assert [country_code(v) for v in ("fr", "France", "Nederland", "Luxembourg", "Metz", None)] == [
        "FR", "FR", "NL", "LU", None, None]
    assert parse_date("2026-09-01 12:17:30 UTC") == datetime(2026, 9, 1, 12, 17, 30, tzinfo=UTC)
    assert parse_date("Thu, 17 Sep 2026 16:06:01 +0200").isoformat() == "2026-09-17T16:06:01+02:00"
    assert parse_date(1758520800000) == datetime(2025, 9, 22, 6, tzinfo=UTC) and parse_date("n/a") is None
    assert format_salary(32000, 38000, "EUR", "year") == "32 000 – 38 000 € / an" and format_salary(None, 0) is None
    assert with_contract_word("développeur", [ContractType.ALTERNANCE]) == "développeur alternance"
    assert with_contract_word("dev en alternance", [ContractType.ALTERNANCE]) == "dev en alternance"
    assert with_contract_word("dev", [ContractType.CDI]) == "dev" and contract_from_hint(None) == ContractType.OTHER


def test_zone_filter(make_job):
    thionville = make_job(location="Thionville (57)", latitude=None, longitude=None, postal_code="57100",
                          raw={"country_code": "FR"})
    assert zones.in_query_zone(thionville, q())  # 27 km from Metz (offline coordinates)
    assert not zones.in_query_zone(thionville, q(location=METZ.model_copy(update={"radius_km": 10})))
    lyon = make_job(location="Lyon", latitude=None, longitude=None, postal_code=None, remote=True)
    assert not zones.in_query_zone(lyon, q()) and zones.in_query_zone(lyon, q(location=None, remote=True))
    village = make_job(location="Village-sur-Exemple", latitude=None, longitude=None, postal_code=None)
    assert not zones.in_query_zone(village, q())  # unknown offline, no geocoder
    assert zones.in_query_zone(village, q(), locate=lambda job: (49.12, 6.18))  # geocoded next to Metz
    lux = make_job(location="Luxembourg, LU", latitude=None, longitude=None, postal_code=None,
                   raw={"country_code": "LU"})
    assert zones.in_query_zone(lux, q(location=LUX)) and not zones.in_query_zone(lux, q())
    assert not zones.in_query_zone(lux, q(location=None, remote=True))
    unknown = make_job(location="", latitude=None, longitude=None, postal_code=None)
    assert zones.in_query_zone(unknown, q(location=None)) and not zones.in_query_zone(unknown, q())
    wttj_abroad = make_job(source="wttj", location="New York, US", remote=True, raw={"office_country": "US"})
    assert zones.offer_country(wttj_abroad) == "US"
    assert not zones.in_query_zone(wttj_abroad, q(location=None, remote=True))
