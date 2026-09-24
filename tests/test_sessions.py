"""Session export (PC) / import (server): storage_state files, cookies added at browser opening (fake contexts)."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobbot import cli, cli_support, sessions
from jobbot.apply import browser
from jobbot.classroom import browser as classroom_browser

FUTURE = time.time() + 30 * 86400
STATE = {
    "cookies": [
        {"name": "li_at", "value": "VALEUR-COOKIE-SECRETE", "domain": ".www.linkedin.com", "path": "/", "expires": FUTURE,
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "old", "value": "x", "domain": ".linkedin.com", "path": "/", "expires": 1000.0},
        {"name": "hw", "value": "b", "domain": "www.hellowork.com", "path": "/", "expires": -1, "sameSite": "Bof"},
        {"name": "SID", "value": "g", "domain": ".google.com", "path": "/", "expires": FUTURE},
        {"name": "tracker", "value": "t", "domain": ".doubleclick.net", "path": "/", "expires": FUTURE},
        {"name": "evil", "value": "e", "domain": "linkedin.com.evil.io", "path": "/"},
    ],
    "origins": [{"origin": "https://www.linkedin.com", "localStorage": [{"name": "k", "value": "v"}]},
                {"origin": "https://ads.example.org", "localStorage": []}],
}


class FakeContext:
    def __init__(self, state: dict | None = None):
        self.state = state or STATE
        self.added: list[dict] = []

    def storage_state(self):
        return self.state

    def add_cookies(self, cookies):
        self.added.extend(cookies)


def _opener(context: FakeContext, calls: list[str]):
    @contextmanager
    def opener(ctx, group):
        calls.append(group)
        yield context
    return opener


def _write(ctx, site: str, cookies: list[dict], *, age_s: float = 0) -> Path:
    path = sessions.session_file(ctx.root, ctx.settings, site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------- pure helpers


def test_site_filtering_and_profiles(ctx):
    kept = sessions.site_cookies(STATE["cookies"], "linkedin")
    assert [c["name"] for c in kept] == ["li_at"]  # expired and look-alike domains dropped
    hello = sessions.site_cookies(STATE["cookies"], "hellowork")[0]
    assert "sameSite" not in hello and hello["expires"] == -1  # invalid sameSite removed, session cookie kept
    assert [o["origin"] for o in sessions.site_state(STATE, "linkedin")["origins"]] == ["https://www.linkedin.com"]
    assert sessions.sites_for_profile(ctx.settings, None) == sessions.MAIN_PROFILE_SITES
    assert sessions.sites_for_profile(ctx.settings, "data/browser-profile-classroom") == ("classroom",)
    assert sessions.sites_for_profile(ctx.settings, "data/autre") == ()
    assert sessions.expand_sites("all") == sessions.SITES and sessions.expand_sites("WTTJ") == ("wttj",)
    with pytest.raises(ValueError, match="Site inconnu"):
        sessions.expand_sites("monster")


# --------------------------------------------------------------------------- export


def test_export_writes_one_file_per_site_and_marks_it_imported(ctx):
    calls: list[str] = []
    lines = sessions.export_sessions(ctx, ["linkedin", "hellowork", "wttj", "classroom"],
                                     opener=_opener(FakeContext(), calls))
    assert calls == ["main", "classroom"]  # one browser opening per profile
    assert any(line.startswith("linkedin : 1 cookie(s) exporté(s)") for line in lines)
    assert any(line.startswith("wttj : aucune session") for line in lines)
    data = json.loads(sessions.session_file(ctx.root, ctx.settings, "linkedin").read_text(encoding="utf-8"))
    assert [c["name"] for c in data["cookies"]] == ["li_at"] and len(data["origins"]) == 1
    assert not sessions.session_file(ctx.root, ctx.settings, "wttj").exists()  # never an empty file
    google = json.loads(sessions.session_file(ctx.root, ctx.settings, "classroom").read_text(encoding="utf-8"))
    assert [c["name"] for c in google["cookies"]] == ["SID"]
    assert not sessions.is_pending(ctx.repo, sessions.session_file(ctx.root, ctx.settings, "linkedin"), "linkedin")


def test_export_reports_a_browser_failure(ctx):
    @contextmanager
    def broken(c, group):
        raise RuntimeError("profil déjà utilisé")
        yield  # pragma: no cover

    lines = sessions.export_sessions(ctx, ["linkedin", "classroom"], opener=broken)
    assert lines == ["linkedin : export impossible (profil déjà utilisé)",
                     "classroom : export impossible (profil déjà utilisé)"]


# --------------------------------------------------------------------------- import at opening


def test_import_only_newer_files_and_only_the_site_domains(ctx):
    path = _write(ctx, "linkedin", STATE["cookies"])
    context = FakeContext()
    assert sessions.import_sessions(ctx, context, sessions.MAIN_PROFILE_SITES) == ["linkedin"]
    assert [c["name"] for c in context.added] == ["li_at"]
    assert sessions.import_sessions(ctx, context, ["linkedin"]) == []  # already imported
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))  # a newer copy arrived
    assert sessions.import_sessions(ctx, FakeContext(), ["linkedin"]) == ["linkedin"]


def test_broken_session_file_never_blocks(ctx, caplog):
    path = sessions.session_file(ctx.root, ctx.settings, "hellowork")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{pas du json", encoding="utf-8")

    class Refusing(FakeContext):
        def add_cookies(self, cookies):
            raise RuntimeError("cookie refusé")

    assert sessions.import_sessions(ctx, FakeContext(), ["hellowork"]) == []
    _write(ctx, "linkedin", STATE["cookies"])
    assert sessions.import_sessions(ctx, Refusing(), ["linkedin"]) == []
    assert "non importée" in caplog.text


def test_open_browser_imports_pending_sessions(ctx, monkeypatch):
    _write(ctx, "linkedin", STATE["cookies"])
    _write(ctx, "classroom", STATE["cookies"])
    contexts: list[FakeContext] = []

    class FakePlaywright:
        def start(self):
            return self

        def stop(self):
            pass

    def fake_launch(pw, user_dir, channel, headless, extra=None):
        context = FakeContext()
        context.close = lambda: None
        contexts.append(context)
        return context

    monkeypatch.setattr(browser, "sync_playwright", FakePlaywright)
    monkeypatch.setattr(browser, "_launch", fake_launch)
    with browser.open_browser(ctx, headless=True, import_sessions=False):
        pass
    with classroom_browser.open_classroom_browser(ctx, headless=True, import_session=False):
        pass
    assert contexts[0].added == [] and contexts[1].added == []
    with browser.open_browser(ctx, headless=True):
        pass
    with classroom_browser.open_classroom_browser(ctx, headless=True):
        pass
    assert [c["name"] for c in contexts[2].added] == ["li_at"]
    assert [c["name"] for c in contexts[3].added] == ["SID"]


# --------------------------------------------------------------------------- copy + status + CLI


def test_copy_session_files_validates(ctx, tmp_path):
    source = tmp_path / "depuis-le-pc"
    source.mkdir()
    (source / "linkedin.json").write_text(json.dumps(STATE), encoding="utf-8")
    (source / "notes.json").write_text("{}", encoding="utf-8")  # ignored: not a site
    lines = sessions.copy_session_files(ctx.root, ctx.settings, source)
    assert lines == ["linkedin : 1 cookie(s) — importée à la prochaine ouverture du navigateur"]
    target = sessions.session_file(ctx.root, ctx.settings, "linkedin")
    assert target.exists() and sessions.is_pending(ctx.repo, target, "linkedin")
    with pytest.raises(sessions.SessionError, match="nom attendu"):
        sessions.copy_session_files(ctx.root, ctx.settings, source / "notes.json")
    (source / "wttj.json").write_text("[]", encoding="utf-8")
    with pytest.raises(sessions.SessionError, match="jobbot session export"):
        sessions.copy_session_files(ctx.root, ctx.settings, source / "wttj.json")
    with pytest.raises(sessions.SessionError, match="Introuvable"):
        sessions.copy_session_files(ctx.root, ctx.settings, tmp_path / "absent")
    status = sessions.session_status(ctx.root, ctx.settings, ctx.repo)
    assert [(i.site, i.cookies, i.pending) for i in status] == [("linkedin", 1, True)]


def test_session_cli(ctx, project_root, monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr(cli_support, "build_app_context", lambda root, dry_run: ctx)
    monkeypatch.setattr(sessions, "_default_opener", _opener(FakeContext(), []))

    def invoke(*args):
        result = runner.invoke(cli.app, ["--root", str(project_root), *args])
        cli_support.teardown_logging()
        return result

    assert "Aucune session" in invoke("session", "import").output
    exported = invoke("session", "export", "linkedin")
    assert exported.exit_code == 0 and "1 cookie(s) exporté(s)" in exported.output and "VALEUR-COOKIE-SECRETE" not in exported.output
    assert "linkedin : 1 cookie(s)" in invoke("session", "import").output
    assert invoke("session", "export", "wttj").exit_code == 1  # nothing to export
    (tmp_path / "classroom.json").write_text(json.dumps(STATE), encoding="utf-8")
    copied = invoke("session", "import", str(tmp_path / "classroom.json"))
    assert copied.exit_code == 0 and "classroom : 1 cookie(s)" in copied.output
    assert invoke("session", "import", str(tmp_path / "absent.json")).exit_code == 1
    assert "[simulation]" in invoke("--dry-run", "session", "export").output
