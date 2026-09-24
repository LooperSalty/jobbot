from __future__ import annotations

import shutil
from datetime import date

import pytest

from jobbot.config import ConfigError, load_profile, load_settings
from jobbot.preferences import (
    SETUP_QUESTIONS,
    apply_answer,
    current_answer,
    effective_mode,
    get_value,
    is_setup_completed,
    mark_setup_completed,
    parse_value,
    secret_present,
    set_many,
    set_secret,
    set_value,
)

from .conftest import PROJECT_ROOT


def _q(key: str):
    return next(q for q in SETUP_QUESTIONS if q.key == key)


def test_parse_value():
    assert parse_value("true") is True
    assert parse_value("12") == 12
    assert parse_value("[cdi, alternance]") == ["cdi", "alternance"]
    assert parse_value("") is None
    assert parse_value(3) == 3


def test_set_value_keeps_comments_and_validates(project_root):
    before = (project_root / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "# quotas journaliers" in before
    assert set_value(project_root, "limits.linkedin_per_day", "9") == 9
    after = (project_root / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "# quotas journaliers" in after
    assert "linkedin_per_day: 9" in after
    assert load_settings(project_root).limits.linkedin_per_day == 9
    assert (project_root / "config" / "settings.yaml.bak").exists()


def test_invalid_value_is_rejected_and_file_untouched(project_root):
    path = project_root / "config" / "settings.yaml"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        set_value(project_root, "mode", "yolo")
    assert path.read_text(encoding="utf-8") == before


def test_profile_keys_and_answers(project_root):
    set_many(project_root, {"profile.target.remote_ok": False, "profile.answers.salary_expectation_cdi_luxembourg": "45000"})
    profile = load_profile(project_root / "config" / "profile.yaml")
    assert profile.target.remote_ok is False
    assert profile.answers["salary_expectation_cdi_luxembourg"] == "45000"
    assert get_value(project_root, "profile.identity.first_name") == "Jean"


def test_luxembourg_toggle_and_contracts(project_root):
    q_lux = _q("loc.luxembourg")
    apply_answer(project_root, q_lux, False)
    assert current_answer(project_root, q_lux) is False
    apply_answer(project_root, q_lux, True)
    assert current_answer(project_root, q_lux) is True
    apply_answer(project_root, _q("loc.luxembourg_contracts"), ["cdi", "cdd"])
    profile = load_profile(project_root / "config" / "profile.yaml")
    lu = next(loc for loc in profile.target.locations if loc.city == "Luxembourg")
    assert lu.country == "LU" and [c.value for c in lu.contract_types] == ["cdi", "cdd"]
    apply_answer(project_root, q_lux, False)
    assert current_answer(project_root, q_lux) is False


def test_metz_radius(project_root):
    q = _q("loc.metz_radius")
    apply_answer(project_root, q, 55)
    assert current_answer(project_root, q) == 55


def test_choice_validation(project_root):
    with pytest.raises(ConfigError):
        apply_answer(project_root, _q("mode"), "fast")
    with pytest.raises(ConfigError):
        apply_answer(project_root, _q("profile.target.contract_types"), ["cdi", "job"])


def test_secrets_written_never_read_back(project_root):
    shutil.copy(PROJECT_ROOT / ".env.example", project_root / ".env.example")
    q = _q("SMTP_PASSWORD")
    assert current_answer(project_root, q) is False
    apply_answer(project_root, q, "abcd efgh ijkl mnop")
    assert current_answer(project_root, q) is True
    apply_answer(project_root, q, "")  # empty answer keeps the secret
    assert secret_present(project_root, "SMTP_PASSWORD")
    text = (project_root / ".env").read_text(encoding="utf-8")
    assert "SMTP_PASSWORD=abcd efgh ijkl mnop" in text and "FRANCE_TRAVAIL_CLIENT_ID=" in text
    with pytest.raises(ConfigError):
        set_secret(project_root, "X", "a\nb")


def test_setup_and_warmup(project_root):
    assert is_setup_completed(project_root) is False
    mark_setup_completed(project_root, today=date(2026, 9, 23))
    assert is_setup_completed(project_root)
    s = load_settings(project_root)
    assert s.setup_date == "2026-09-23"
    assert effective_mode(s, today=date(2026, 9, 24)) == "review"
    assert effective_mode(s, today=date(2026, 9, 26)) == "auto"
    assert effective_mode(s.model_copy(update={"mode": "review"}), today=date(2026, 12, 1)) == "review"


def test_every_question_has_a_readable_current_value(project_root):
    for q in SETUP_QUESTIONS:
        current_answer(project_root, q)  # must not raise
