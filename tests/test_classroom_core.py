"""Classroom building blocks: identifiers, page parsing, attachment / link contents, watcher state, settings."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from jobbot import clock
from jobbot.classroom import content, dom, state
from jobbot.classroom.models import (
    Attachment,
    ClassroomPost,
    decode_id,
    encode_id,
    parse_course_url,
    parse_course_urls,
    post_id_from_url,
    stream_fingerprint,
    text_hash,
)
from jobbot.config import ClassroomSettings, ConfigError, Settings, load_secrets, load_settings, parse_active_hours
from jobbot.preferences import SETUP_QUESTIONS, apply_answer, current_answer

COURSE_URL = "https://classroom.google.com/c/MTIzNDU2Nzg5MDEy"
COURSE = parse_course_url(COURSE_URL)
POST_URL = f"{COURSE_URL}/p/NTU1MDAwMTExMjIy"


def _post(**updates) -> ClassroomPost:
    base = dict(post_id="p-555000111222", course_id="123456789012", url=POST_URL, text="Offre d'alternance")
    return ClassroomPost(**{**base, **updates})


# --------------------------------------------------------------------------- identifiers


def test_course_urls_and_ids():
    assert COURSE is not None and COURSE.course_id == "123456789012" and COURSE.key == "MTIzNDU2Nzg5MDEy"
    assert parse_course_url("https://classroom.google.com/u/1/c/MTIzNDU2Nzg5MDEy/p/x?cjc=abc").url == COURSE_URL
    assert parse_course_url("https://evil.example/c/MTIzNDU2Nzg5MDEy") is None
    assert parse_course_url("https://classroom.google.com/h") is None
    courses, invalid = parse_course_urls(f"{COURSE_URL}, {COURSE_URL}?x=1 ; pas-un-lien")
    assert len(courses) == 1 and invalid == 1
    assert parse_course_urls(None) == ([], 0)
    assert decode_id(encode_id("555000111222")) == "555000111222" and decode_id("ABC!") == "ABC!"
    assert decode_id("42") == "42"
    assert post_id_from_url(POST_URL) == "p-555000111222"
    assert post_id_from_url(f"{COURSE_URL}/sa/NTU1MDAwMTExMjIy") == "p-555000111222"
    assert post_id_from_url(f"{COURSE_URL}/a/NTU1MDAwMTExMjMz/details") == "a-555000111233"
    assert post_id_from_url(COURSE_URL) is None


def test_fingerprints_ignore_dates_and_comment_counts():
    assert stream_fingerprint("Mme X 12 sept.\nOffre\n2 commentaires") == stream_fingerprint("Mme X 13 sept.\nOffre")
    assert stream_fingerprint("Offre A") != stream_fingerprint("Offre B")
    assert text_hash(_post()) != text_hash(_post(text="Offre modifiée"))
    assert text_hash(_post()) != text_hash(_post(attachments=[Attachment(url="https://docs.google.com/x")]))


# --------------------------------------------------------------------------- page parsing


def test_login_wall_and_no_access():
    assert dom.login_wall("https://accounts.google.com/v3/signin/identifier?continue=x", "")
    assert dom.login_wall("https://edu.google.com/intl/ALL_fr/workspace-for-education/classroom/", "")
    assert dom.login_wall(COURSE_URL, "Choisissez un compte pour continuer vers Classroom")
    assert not dom.login_wall(COURSE_URL, "Choisissez un compte", has_posts=True)
    assert not dom.login_wall(COURSE_URL, "Rappel : réunion jeudi")
    assert dom.no_access("Vous n'avez pas accès à ce cours") and not dom.no_access("Flux")


def test_parse_post_text_variants():
    author, when, body = dom.parse_post_text(
        "Flux\nMme Claire Exemple\n12 sept. (Modifié : 13 sept.)\nmore_vert\nOffre chez Acme\nVoir plus\n"
        "Commentaires ajoutés au cours\nJean : merci")
    assert (author, when, body) == ("Mme Claire Exemple", "12 sept. (Modifié : 13 sept.)", "Offre chez Acme")
    author, when, body = dom.parse_post_text("Mme Claire Exemple 12 sept. more_vert\nTexte\n2 commentaires de la classe")
    assert (author, when, body) == ("Mme Claire Exemple", "12 sept.", "Texte")
    assert dom.parse_post_text("Juste un texte sans date") == ("", "", "Juste un texte sans date")
    assert dom.parse_post_text("Paul Exemple\nSep 12, 2026\nHello")[1] == "Sep 12, 2026"
    assert not dom.is_date_line("Les entretiens auront lieu le 12 oct. dans nos locaux de Metz, venez nombreux")


def test_parse_date_text():
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    assert clock.to_paris(dom.parse_date_text("12 sept.", now)).date().isoformat() == "2026-09-12"
    assert clock.to_paris(dom.parse_date_text("20 déc.", now)).year == 2025  # a future date is last year's
    assert clock.to_paris(dom.parse_date_text("Sep 12, 2024", now)).year == 2024
    assert (now - dom.parse_date_text("hier", now)).days == 1
    assert dom.parse_date_text("09:41", now) is not None
    assert dom.parse_date_text("31 févr.", now) is None and dom.parse_date_text("bientôt", now) is None


def test_classify_links():
    links, files = dom.classify_links([
        {"href": "https://www.google.com/url?q=https://jobs.example.org/42&sa=D", "text": "offre"},
        {"href": "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit", "text": "Fiche", "label": "PJ : Fiche"},
        {"href": "https://drive.google.com/file/d/1ZyXwVuTsRqPoNm/view?usp=sharing", "text": "offre.pdf"},
        {"href": "https://drive.google.com/open?id=1ZyXwVuTsRqPoNm", "text": "doublon"},
        {"href": "https://forms.gle/AbCd123", "text": "formulaire"},
        {"href": "https://classroom.google.com/c/MTIzNDU2Nzg5MDEy", "text": "cours"},
        {"href": "mailto:rh@acme.example", "text": "mail"},
    ])
    assert links == ["https://jobs.example.org/42", "https://forms.gle/AbCd123"]
    assert [(f.kind, f.file_id, f.title) for f in files] == [("doc", "1AbCdEfGhIjKlMnOp", "PJ : Fiche"),
                                                             ("drive", "1ZyXwVuTsRqPoNm", "offre.pdf")]
    assert dom.google_file("https://docs.google.com/spreadsheets/d/1SheetIdValue01/edit")[0] == "sheet"
    assert dom.unwrap_google_redirect("https://example.org/a") == "https://example.org/a"


def test_build_post_prefers_the_post_page():
    card = {"href": POST_URL, "text": "Mme Claire Exemple 12 sept.\nOffre tronquée…",
            "links": [{"href": POST_URL, "text": "12 sept."}]}
    page = {"text": "Mme Claire Exemple\n12 sept.\nOffre complète : Acme recrute.",
            "links": [{"href": "https://www.google.com/url?q=https://acme.example/jobs", "text": "site"}]}
    post = dom.build_post(COURSE, card, page, now=datetime(2026, 9, 24, tzinfo=UTC))
    assert post.post_id == "p-555000111222" and post.url == POST_URL and post.author == "Mme Claire Exemple"
    assert post.text == "Offre complète : Acme recrute." and post.links == ["https://acme.example/jobs"]
    assert post.published_at is not None and post.stream_hash
    fallback = dom.build_post(COURSE, {"href": "", "text": "Mme X\n3 sept.\nTexte d'une publication"})
    assert fallback.post_id.startswith("h-") and fallback.url == COURSE_URL and fallback.author == "Mme X"


# --------------------------------------------------------------------------- attachments & links


class FakeDownloader:
    def __init__(self, files: dict[str, tuple[str, bytes]] | None = None, error: Exception | None = None):
        self.files = files or {}
        self.error = error

    def download(self, attachment: Attachment):
        if self.error:
            raise self.error
        return self.files.get(attachment.file_id or "")


def _blank_pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_bytes_to_text_and_attachments():
    assert content.bytes_to_text("text/plain; charset=utf-8", "Offre : développeur".encode()) == "Offre : développeur"
    assert content.bytes_to_text("text/html", b"<p>Bonjour</p><script>x</script>") == "Bonjour"
    assert "Missions" in content.bytes_to_text("application/octet-stream", _docx_bytes("Missions : API REST"))
    assert content.bytes_to_text("application/pdf", _blank_pdf_bytes()) == ""
    assert content.bytes_to_text("application/pdf", b"%PDF-broken") == ""
    assert content.bytes_to_text("image/png", b"\x89PNG") == ""
    doc = Attachment(title="Fiche", url="https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit", kind="doc",
                     file_id="1AbCdEfGhIjKlMnOp")
    assert content.google_export_url(doc) == "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/export?format=txt"
    assert content.google_export_url(Attachment(url="https://x", kind="other")) is None
    filled = content.with_attachment_texts(_post(attachments=[doc]),
                                           FakeDownloader({"1AbCdEfGhIjKlMnOp": ("text/plain", b"Poste : dev")}))
    assert filled.attachments[0].text == "Poste : dev"
    assert content.attachment_text(doc, FakeDownloader(error=RuntimeError("réseau"))) == ""
    huge = FakeDownloader({"1AbCdEfGhIjKlMnOp": ("text/plain", b"x" * (content.MAX_DOWNLOAD_BYTES + 1))})
    assert content.attachment_text(doc, huge) == ""


def test_post_urls_dedup_and_skip_google_files():
    post = _post(text="Postuler : https://jobs.example.org/42. Fiche https://docs.google.com/document/d/1AbCdEfGhIjKl/edit",
                 links=["https://jobs.example.org/42"])
    assert content.post_urls(post) == ["https://jobs.example.org/42"]


@respx.mock
def test_link_contents_boards_pages_and_errors(ctx):
    respx.get("https://jobs.example.org/42").mock(return_value=httpx.Response(
        200, html="<h1>Développeur Python</h1><p>Chez Delta Data</p>"))
    respx.get("https://down.example.org/x").mock(return_value=httpx.Response(503))
    post = _post(links=["https://jobs.example.org/42", "https://down.example.org/x",
                        "https://www.linkedin.com/jobs/view/4410135372/", "https://forms.gle/AbCd123"])
    seen = []

    def board(c, url, hit):
        seen.append((hit.source, hit.external_id))
        return content.LinkContent(url=url, kind="board", text="LinkedIn : dev")

    got = content.link_contents(ctx, post, board_reader=board)
    assert [c.kind for c in got] == ["page", "error", "board"]
    assert "Développeur Python" in got[0].text and "503" in got[1].text
    assert seen == [("linkedin", "4410135372")]


def test_board_content_reads_the_source_without_storing(ctx, monkeypatch, make_job):
    import jobbot.sources.registry as registry
    from jobbot.sources.alerts import match_offer_url

    url = "https://www.linkedin.com/jobs/view/4410135372/"
    full = make_job(source="linkedin", external_id="4410135372", title="Dev React", description="Missions…", url=url)
    asked = []
    monkeypatch.setattr(registry, "fetch_details_only", lambda c, job: asked.append(job) or full)
    got = content.board_content(ctx, url, match_offer_url(url))
    assert got.board == full and "Dev React" in got.text and ctx.repo.get_job(full.id) is None
    assert asked[0].source == "linkedin" and asked[0].external_id == "4410135372" and asked[0].title == ""
    expired = full.model_copy(update={"status": "expired"})
    monkeypatch.setattr(registry, "fetch_details_only", lambda c, job: expired)
    assert "expirée" in content.board_content(ctx, url, match_offer_url(url)).text


# --------------------------------------------------------------------------- state, settings, wizard


def test_seen_state_is_immutable_and_bounded(repo):
    seen = state.load_seen(repo, "c1")
    assert seen == {} and state.wants(seen, "p-1", "fp")
    updated = state.remember(seen, "p-1", s="fp", h="h1")
    assert seen == {} and not state.wants(updated, "p-1", "fp") and state.wants(updated, "p-1", "fp2")
    assert state.already_processed(updated, "p-1", "h1") and not state.already_processed(updated, "p-1", "h2")
    state.save_seen(repo, "c1", {f"p-{i}": {"at": f"2026-09-{i % 28 + 1:02d}"} for i in range(state.MAX_REMEMBERED + 5)})
    assert len(state.load_seen(repo, "c1")) == state.MAX_REMEMBERED
    repo.kv_set(state.POSTS_KEY.format(course="c2"), "pas du json")
    assert state.load_seen(repo, "c2") == {} and state.failures({"p": {"f": 2}}, "p") == 2


def test_active_hours_lock_last_check_and_login_alert(ctx, repo):
    paris = datetime(2026, 9, 24, 12, 0, tzinfo=clock.PARIS)
    assert state.in_active_hours("07:30-21:00", paris)
    assert not state.in_active_hours("07:30-21:00", paris.replace(hour=22))
    assert state.in_active_hours("22:00-06:00", paris.replace(hour=23)) and not state.in_active_hours("22:00-06:00", paris)
    with state.classroom_lock(ctx):
        with pytest.raises(state.ClassroomBusy), state.classroom_lock(ctx):
            pass
    assert not (ctx.root / "data" / state.LOCK_FILE).exists()
    assert state.last_check(repo) is None
    state.record_check(repo, ok=False, detail="connexion", login_required=True)
    assert state.last_check(repo)["login_required"] is True
    assert state.login_alert_due(repo, paris) and not state.login_alert_due(repo, paris)
    assert state.login_alert_due(repo, paris.replace(hour=20))


def test_settings_and_secrets(project_root):
    assert Settings().classroom == ClassroomSettings()
    assert parse_active_hours("7:05-21:00")[0].minute == 5
    with pytest.raises(ValidationError):
        ClassroomSettings(active_hours="matin")
    with pytest.raises(ValidationError):
        ClassroomSettings(poll_minutes=0)
    (project_root / ".env").write_text(f"CLASSROOM_COURSE_URLS={COURSE_URL}\nSMTP_USER=\n", encoding="utf-8")
    assert load_secrets(project_root).classroom_course_urls == COURSE_URL
    (project_root / ".env").write_text("CLASSROOM_COURSE_URLS=   # lien du cours\n", encoding="utf-8")
    assert load_secrets(project_root).classroom_course_urls is None


def test_wizard_questions_classroom_section(project_root):
    questions = {q.key: q for q in SETUP_QUESTIONS if q.section == "Offres de l'école (Classroom)"}
    assert set(questions) == {"classroom.enabled", "classroom.auto_apply", "classroom.poll_minutes",
                              "CLASSROOM_COURSE_URLS"}
    apply_answer(project_root, questions["classroom.poll_minutes"], 10)
    apply_answer(project_root, questions["classroom.auto_apply"], False)
    classroom = load_settings(project_root).classroom
    assert classroom.poll_minutes == 10 and classroom.auto_apply is False
    with pytest.raises(ConfigError):
        apply_answer(project_root, questions["classroom.poll_minutes"], 0)
    secret = questions["CLASSROOM_COURSE_URLS"]
    assert current_answer(project_root, secret) is False
    with pytest.raises(ConfigError):
        apply_answer(project_root, secret, "https://exemple.org/pas-classroom")
    apply_answer(project_root, secret, f"{COURSE_URL}/p/NTU1MDAwMTExMjIy?cjc=abc")
    env = (project_root / ".env").read_text(encoding="utf-8")
    assert f"CLASSROOM_COURSE_URLS={COURSE_URL}\n" in env and current_answer(project_root, secret) is True
