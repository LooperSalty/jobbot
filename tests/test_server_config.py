"""config/settings.local.yaml (machine overrides), where preferences write, Claude OAuth token passthrough."""

from __future__ import annotations

import subprocess

import pytest

from jobbot import llm
from jobbot.config import ConfigError, LLMSettings, Secrets, Settings, deep_merge, load_settings
from jobbot.preferences import get_value, set_many, set_value

LOCAL = "config/settings.local.yaml"


def _local(root, text: str) -> None:
    (root / LOCAL).write_text(text, encoding="utf-8")


def test_deep_merge_is_pure_and_nested():
    base = {"llm": {"provider": "claude_cli", "model": "m"}, "limits": [1], "headless": False}
    merged = deep_merge(base, {"llm": {"provider": "anthropic"}, "limits": [2], "browser_channel": None})
    assert merged == {"llm": {"provider": "anthropic", "model": "m"}, "limits": [2], "headless": False,
                      "browser_channel": None}
    assert base["llm"]["provider"] == "claude_cli"  # never mutated


def test_local_file_wins_over_settings_yaml(project_root):
    assert load_settings(project_root).headless is False
    _local(project_root, "headless: true\nbrowser_channel: null\nllm:\n  provider: none\n"
                         "classroom:\n  poll_minutes: 5\nnotify_email: true\n")
    settings = load_settings(project_root)
    assert settings.headless is True and settings.browser_channel is None and settings.notify_email
    assert settings.llm.provider == "none" and settings.llm.model == "claude-sonnet-5"  # sibling keys kept
    assert settings.classroom.poll_minutes == 5 and settings.limits.linkedin_per_day == 15
    assert load_settings(project_root, include_local=False).headless is False


def test_invalid_local_file_is_reported(project_root):
    _local(project_root, "headless: [\n")
    with pytest.raises(ConfigError, match="settings.local.yaml"):
        load_settings(project_root)
    _local(project_root, "- a\n- b\n")
    with pytest.raises(ConfigError):
        load_settings(project_root)


def test_preferences_write_settings_yaml_unless_the_key_is_local(project_root):
    _local(project_root, "# serveur\nheadless: true\nclassroom:\n  poll_minutes: 5\n")
    settings_yaml = project_root / "config" / "settings.yaml"
    before = settings_yaml.read_text(encoding="utf-8")
    assert get_value(project_root, "headless") is True  # effective value
    set_value(project_root, "headless", "false")  # set in the local file → updated there
    assert settings_yaml.read_text(encoding="utf-8") == before
    assert "headless: false" in (project_root / LOCAL).read_text(encoding="utf-8")
    assert "# serveur" in (project_root / LOCAL).read_text(encoding="utf-8")  # comments kept
    set_many(project_root, {"classroom.poll_minutes": 7, "limits.linkedin_per_day": 9})
    assert "poll_minutes: 7" in (project_root / LOCAL).read_text(encoding="utf-8")
    assert "linkedin_per_day: 9" in settings_yaml.read_text(encoding="utf-8")
    settings = load_settings(project_root)
    assert (settings.headless, settings.classroom.poll_minutes, settings.limits.linkedin_per_day) == (False, 7, 9)
    with pytest.raises(ConfigError):
        set_value(project_root, "headless", "peut-être")
    set_value(project_root, "classroom.max_posts", "12")  # sibling not in the local file → settings.yaml
    assert "max_posts: 12" in settings_yaml.read_text(encoding="utf-8")


def test_notification_settings_validation():
    assert Settings(notify_ntfy_topic=" jobbot-alertes_42 ").notify_ntfy_topic == "jobbot-alertes_42"
    assert Settings(notify_ntfy_topic="").notify_ntfy_topic is None
    with pytest.raises(ValueError, match="ntfy"):
        Settings(notify_ntfy_topic="../admin")
    with pytest.raises(ValueError):
        Settings(notify_email_max_per_day=-1)
    secrets = Secrets(_env_file=None, telegram_bot_token="  # commentaire", claude_code_oauth_token="")
    assert secrets.telegram_bot_token is None and secrets.claude_code_oauth_token is None


def test_claude_cli_receives_the_oauth_token(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/claude")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": "ok"}', stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    client = llm.build_llm(LLMSettings(), Secrets(_env_file=None, claude_code_oauth_token="jeton-test"))
    assert client.complete("bonjour") == "ok"
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "jeton-test"
    llm.ClaudeCLIClient(LLMSettings()).complete("x")
    assert seen["env"] is None  # no token: the subprocess inherits the environment unchanged
