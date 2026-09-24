"""Playwright browser session (persistent, logged-in profile) + helpers shared by the browser appliers.

Never automates login, captcha, OTP or security checkpoints: ``login`` opens the page and waits for the
human; ``detect_blocker`` lets appliers stop and hand over with a screenshot.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from jobbot import clock
from jobbot.context import AppContext
from jobbot.pacing import Pacer
from jobbot.textutil import slugify, truncate

log = logging.getLogger(__name__)

Site = Literal["linkedin", "hellowork", "wttj"]

LOGIN_URLS: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/login",
    "hellowork": "https://www.hellowork.com/fr-fr/candidat/connexion-inscription.html#connexion",
    "wttj": "https://www.welcometothejungle.com/fr/signin",
}
LOGIN_POLL_S = 2.0
VIEWPORT = {"width": 1366, "height": 900}

CHECKPOINT_URL_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "linkedin.com/login")
LOGIN_URL_RE = re.compile(r"connexion-inscription|/signin|/sign-in|/login|/connexion|/identification", re.I)
CHECKPOINT_TEXT_RE = re.compile(
    r"v[ée]rifi(ez|cation de) votre identit[ée]|verify your identity|security (check|verification)|"
    r"contr[ôo]le de s[ée]curit[ée]|[êe]tes-vous un robot|are you a robot|let.?s do a quick security check",
    re.I,
)
CAPTCHA_SEL = (
    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='hcaptcha.com'], iframe[src*='challenges.cloudflare.com'], "
    "iframe[src*='arkoselabs'], iframe[title*='captcha' i], .frc-captcha, #captcha, #challenge-form, "
    "[data-callback][data-sitekey]"
)
OTP_SEL = "input[autocomplete='one-time-code'], [data-controller~='otp'], input[name*='otp' i]"
PASSWORD_SEL = "input[type='password']"
COOKIE_BUTTONS = (
    "#hw-cc-notice-continue-without-accepting-btn", "#onetrust-reject-all-handler", "#didomi-notice-disagree-button",
    "#axeptio_btn_dismiss", "#tarteaucitronAllDenied2", "button.cc-deny",
    "button:has-text('Continuer sans accepter')", "button:has-text('Tout refuser')", "button:has-text('Reject all')",
)
HELLOWORK_ACCOUNT_JS = """async () => {
  try {
    const r = await fetch('/fr-fr/compte/accountdata', {method: 'POST',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({shouldFetchBookmarks: false})});
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}"""
ALLOWED_UPLOAD_SUFFIXES = frozenset({".pdf", ".doc", ".docx", ".odt", ".rtf"})

_sleep = time.sleep
_monotonic = time.monotonic


# --------------------------------------------------------------------------- session


def _launch(pw: Playwright, user_dir: Path, channel: str | None, headless: bool) -> BrowserContext:
    kwargs: dict[str, Any] = dict(
        user_data_dir=str(user_dir), headless=headless, locale="fr-FR", timezone_id="Europe/Paris",
        viewport=VIEWPORT, accept_downloads=True,
    )
    if channel:
        try:
            return pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        except PlaywrightError as exc:
            log.warning("Navigateur « %s » indisponible (%s) : repli sur le Chromium de Playwright.", channel,
                        truncate(str(exc), 200))
    return pw.chromium.launch_persistent_context(**kwargs)


@contextmanager
def open_browser(ctx: AppContext, *, headless: bool | None = None) -> Iterator[BrowserContext]:
    """Persistent browser context (profile in ``settings.paths.browser_profile``), closed on exit."""
    user_dir = ctx.path(ctx.settings.paths.browser_profile + "/.keep").parent
    user_dir.mkdir(parents=True, exist_ok=True)
    head = ctx.settings.headless if headless is None else headless
    pw = sync_playwright().start()
    context: BrowserContext | None = None
    try:
        context = _launch(pw, user_dir, ctx.settings.browser_channel, head)
        yield context
    finally:
        if context is not None:
            try:
                context.close()
            except PlaywrightError as exc:
                log.debug("Fermeture du navigateur : %s", exc)
        pw.stop()


def login(ctx: AppContext, site: Site, timeout_s: int = 600) -> bool:
    """Open the site's login page in a visible window and wait until the human is logged in."""
    if site not in LOGIN_URLS:
        raise ValueError(f"Site inconnu : {site} (attendu : {', '.join(LOGIN_URLS)})")
    with open_browser(ctx, headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(LOGIN_URLS[site], wait_until="domcontentloaded")
        except PlaywrightError as exc:
            log.error("Impossible d'ouvrir la page de connexion %s : %s", site, exc)
            return False
        log.info("Connecte-toi à %s dans la fenêtre ouverte (jamais automatisé). Attente max %d s…", site, timeout_s)
        deadline = _monotonic() + timeout_s
        while _monotonic() < deadline:
            if page.is_closed():
                log.warning("Fenêtre fermée avant la connexion à %s.", site)
                return False
            if is_logged_in(page, site):
                log.info("Connexion à %s détectée : session enregistrée dans le profil navigateur.", site)
                return True
            _sleep(LOGIN_POLL_S)
    log.warning("Connexion à %s non détectée après %d s.", site, timeout_s)
    return False


def is_logged_in(page: Page, site: Site) -> bool:
    """Passive check (cookies / DOM / read-only account probe); never navigates."""
    try:
        if site == "linkedin":
            return _linkedin_logged_in(page)
        if site == "hellowork":
            return _hellowork_logged_in(page)
        if site == "wttj":
            return _wttj_logged_in(page)
    except PlaywrightError as exc:
        log.debug("Vérification de session %s impossible : %s", site, exc)
        return False
    raise ValueError(f"Site inconnu : {site}")


def _has_cookie(page: Page, url: str, pattern: str) -> bool:
    regex = re.compile(pattern, re.I)
    return any(regex.search(c.get("name", "")) and c.get("value") for c in page.context.cookies(url))


def _linkedin_logged_in(page: Page) -> bool:
    if any(marker in page.url for marker in CHECKPOINT_URL_MARKERS):
        return False
    return _has_cookie(page, "https://www.linkedin.com", r"^li_at$")


def _hellowork_logged_in(page: Page) -> bool:
    if page.locator("body.isConnected").count():
        return True
    if "hellowork.com" not in page.url:
        return False
    data = page.evaluate(HELLOWORK_ACCOUNT_JS)
    return bool(isinstance(data, dict) and data.get("IsAuthenticated"))


def _wttj_logged_in(page: Page) -> bool:
    # Heuristic (not verified live): session cookie or a user-menu link, and not on the sign-in page.
    if LOGIN_URL_RE.search(page.url):
        return False
    if _has_cookie(page, "https://www.welcometothejungle.com", r"session|auth|token"):
        return True
    return page.locator("a[href*='/me/'], a[href$='/me'], [data-testid*='user-menu' i]").count() > 0


def human_pause(pacer: Pacer) -> None:
    """Short human-like pause between two UI actions (settings.limits.action_min_ms..max_ms)."""
    pacer.between_actions()


# --------------------------------------------------------------------------- helpers for appliers


def detect_blocker(page: Page, *, captcha: bool = True) -> tuple[str, str] | None:
    """(kind, French message) when a login wall, checkpoint, captcha or OTP blocks automation."""
    url = page.url or ""
    if any(marker in url for marker in CHECKPOINT_URL_MARKERS[:2]):
        return "checkpoint", "vérification de sécurité demandée (checkpoint)"
    text = body_text(page, limit=4000)
    if CHECKPOINT_TEXT_RE.search(text):
        return "checkpoint", "vérification de sécurité / d'identité demandée"
    if captcha and _any_visible(page, CAPTCHA_SEL):
        return "captcha", "captcha affiché"
    if _any_visible(page, OTP_SEL):
        return "otp", "code de vérification (OTP) demandé"
    if LOGIN_URL_RE.search(url) or any(m in url for m in CHECKPOINT_URL_MARKERS[2:]) or _any_visible(page, PASSWORD_SEL):
        return "login", "connexion requise"
    return None


def _any_visible(scope: Page | Locator, selector: str) -> bool:
    try:
        loc = scope.locator(selector)
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
    except PlaywrightError:
        return False


def first_visible(scope: Any, selectors: Sequence[str]) -> Locator | None:
    """First visible & enabled element among ``selectors`` (no waiting: callers wait beforehand)."""
    for selector in selectors:
        try:
            loc = scope.locator(selector)
            for i in range(min(loc.count(), 5)):
                item = loc.nth(i)
                if item.is_visible() and item.is_enabled():
                    return item
        except PlaywrightError:
            continue
    return None


def body_text(scope: Any, *, limit: int = 20000) -> str:
    try:
        target = scope.locator("body") if isinstance(scope, Page) else scope
        return target.first.inner_text(timeout=3000)[:limit]
    except PlaywrightError:
        return ""


def dismiss_cookie_banner(page: Page) -> bool:
    """Refuse / continue without accepting when a known consent banner is shown (best effort)."""
    button = first_visible(page, COOKIE_BUTTONS)
    if button is None:
        return False
    try:
        button.click(timeout=3000)
        return True
    except PlaywrightError as exc:
        log.debug("Bandeau cookies non fermé : %s", exc)
        return False


def screenshot(ctx: AppContext, page: Page, name: str) -> str | None:
    """Full-page screenshot in ``<logs_dir>/screens/`` (never raises)."""
    try:
        stamp = clock.to_paris(clock.now()).strftime("%Y%m%d-%H%M%S")
        path = ctx.path(f"{ctx.settings.paths.logs_dir}/screens/{stamp}_{slugify(name, 80)}.png")
        page.screenshot(path=str(path), full_page=True, timeout=15000)
        return str(path)
    except (PlaywrightError, OSError) as exc:
        log.warning("Capture d'écran impossible (%s) : %s", name, exc)
        return None


def check_upload(path: str | Path | None, max_bytes: int) -> str | None:
    """French problem description when a document cannot be uploaded, else ``None``."""
    if not path:
        return "aucun document à joindre"
    p = Path(path)
    if not p.is_file():
        return f"document introuvable : {p}"
    if p.suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
        return f"format non accepté : {p.name}"
    size = p.stat().st_size
    if size > max_bytes:
        mb = f"{size / 1048576:.1f}".replace(".", ",")
        limit = f"{max_bytes / 1048576:.0f}"
        return f"document trop volumineux : {p.name} ({mb} Mo > {limit} Mo)"
    return None


def fit_text(text: str, max_chars: int) -> str:
    """Cut ``text`` to ``max_chars`` at a sentence end when possible (0 = no limit)."""
    text = (text or "").strip()
    if not max_chars or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(". "), head.rfind(".\n"), head.rfind("! "), head.rfind("? "), head.rfind("\n\n"))
    return head[: cut + 1].rstrip() if cut >= max_chars * 0.6 else truncate(text, max_chars)


def page_of(scope: Any) -> Page:
    """The Page behind a Page, Frame or Locator."""
    return scope if isinstance(scope, Page) else scope.page
