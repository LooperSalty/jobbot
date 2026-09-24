"""Shared fixtures. Tests never touch the network, the real DB, a real browser or a real mailbox."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.config import load_profile, load_settings
from jobbot.context import AppContext
from jobbot.db import Repository
from jobbot.llm import FakeLLM
from jobbot.models import ApplyMethod, Company, ContactEmail, ContractType, JobOffer
from jobbot.textutil import stable_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PROFILE_FIXTURE = FIXTURES / "profile.yaml"
"""Fictitious candidate profile: tests never read the owner's private config/profile.yaml (gitignored)."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tests marked ``live`` hit real services: skipped unless the -m expression selects them."""
    expr = config.getoption("markexpr") or ""
    if "live" in expr and "not live" not in expr:
        return
    skip_live = pytest.mark.skip(reason="test réseau : lancer avec -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A throw-away copy of the project config (fictitious profile + settings), no .env."""
    (tmp_path / "config").mkdir()
    shutil.copy(PROFILE_FIXTURE, tmp_path / "config" / "profile.yaml")
    shutil.copy(PROJECT_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def profile():
    return load_profile(PROFILE_FIXTURE)


@pytest.fixture
def settings():
    return load_settings(PROJECT_ROOT)


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "test.db")
    yield r
    r.close()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM("{}")


@pytest.fixture
def ctx(project_root: Path, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    for var in ("SMTP_USER", "SMTP_PASSWORD", "FRANCE_TRAVAIL_CLIENT_ID", "LBA_API_TOKEN", "HUNTER_API_KEY",
                "CLASSROOM_COURSE_URLS"):
        monkeypatch.delenv(var, raising=False)
    c = AppContext.build(project_root, dry_run=True, llm=fake_llm)
    yield c
    c.repo.close()


@pytest.fixture
def make_job() -> Callable[..., JobOffer]:
    def _make(**overrides) -> JobOffer:
        external_id = overrides.pop("external_id", "ext-1")
        source = overrides.pop("source", "france_travail")
        base = dict(
            id=stable_id(source, external_id),
            source=source,
            external_id=external_id,
            title="Développeur Full-Stack React / Node.js en alternance (H/F)",
            company="Acme Logiciels",
            location="Metz (57)",
            postal_code="57000",
            latitude=49.1193,
            longitude=6.1757,
            description=(
                "Nous recherchons un développeur full-stack en alternance (Bac+5) pour 24 mois. "
                "Stack : TypeScript, React, Next.js, Node.js, PostgreSQL, Docker, Git. "
                "Connaissances appréciées : CI/CD, tests unitaires, méthodes agiles (Scrum). "
                "Qualités : autonomie, rigueur, travail en équipe."
            ),
            url="https://example.org/offre/1",
            apply_method=ApplyMethod.EMAIL,
            contract_type=ContractType.ALTERNANCE,
            contact_email="rh@acme-logiciels.fr",
            found_at=clock.now(),
        )
        base.update(overrides)
        return JobOffer(**base)

    return _make


@pytest.fixture
def make_company() -> Callable[..., Company]:
    def _make(**overrides) -> Company:
        base = dict(
            id=stable_id("company", overrides.get("siren", "123456789")),
            name="Acme Logiciels",
            siren="123456789",
            naf_code="62.01Z",
            city="Metz",
            postal_code="57000",
            domain="acme-logiciels.fr",
            website="https://www.acme-logiciels.fr",
            contacts=[ContactEmail(email="recrutement@acme-logiciels.fr", confidence=0.9, kind="recruitment")],
            found_at=clock.now(),
        )
        base.update(overrides)
        return Company(**base)

    return _make
