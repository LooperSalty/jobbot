"""Headless Chromium (Playwright sync API) for HTML → tagged A4 PDF, with page measurement for auto-fit.

Bundled Chromium is used on purpose (no user profile, no extension, deterministic fonts). When the calling
thread already runs an asyncio loop (e.g. another sync Playwright session is open), the work is moved to a
worker thread, since two sync Playwright instances cannot share a thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, TypeVar

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from jobbot.documents._fit import BISECTION_STEPS, Density
from jobbot.documents.models import DocumentError

log = logging.getLogger(__name__)

T = TypeVar("T")
A4_HEIGHT_PX = 297 / 25.4 * 96  # 1122.52 CSS px
FIT_MARGIN_PX = 3.0
VIEWPORT = {"width": 794, "height": 1123}
_APPLY_AND_MEASURE = """(vars) => {
  const root = document.documentElement;
  for (const [key, value] of Object.entries(vars)) root.style.setProperty(key, value);
  const page = document.querySelector('.page');
  return page ? page.getBoundingClientRect().height : document.body.scrollHeight;
}"""


def run_isolated(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call ``fn`` here, or in a worker thread when an asyncio loop is running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobbot-pdf") as pool:
        return pool.submit(fn, *args, **kwargs).result()


@contextmanager
def chromium_page() -> Iterator[Any]:
    """A print-emulated A4-wide page in headless bundled Chromium."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport=VIEWPORT)
                page.emulate_media(media="print")
                yield page
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise DocumentError(
            f"Rendu PDF impossible avec Chromium (Playwright) : {exc}. Lancez « playwright install chromium »."
        ) from exc


def _fits(page: Any, density: Density, t: float) -> bool:
    return float(page.evaluate(_APPLY_AND_MEASURE, density.at(t))) <= A4_HEIGHT_PX - FIT_MARGIN_PX


def fit_density(page: Any, density: Density, steps: int = BISECTION_STEPS) -> float | None:
    """Airiest density (smallest t in [0, 1]) whose content fits one A4 page, left applied on the page;
    None (tightest applied) when even t=1 overflows."""
    if _fits(page, density, 0.0):
        return 0.0
    if not _fits(page, density, 1.0):
        return None
    low, high = 0.0, 1.0  # low overflows, high fits
    for _ in range(steps):
        mid = (low + high) / 2
        if _fits(page, density, mid):
            high = mid
        else:
            low = mid
    page.evaluate(_APPLY_AND_MEASURE, density.at(high))
    return high


def load_html(page: Any, html: str) -> None:
    page.set_content(html, wait_until="load")


def print_pdf(page: Any) -> bytes:
    return page.pdf(
        format="A4", print_background=True, tagged=True, outline=False, display_header_footer=False,
        prefer_css_page_size=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
    )


def page_count(data: bytes) -> int:
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except (PdfReadError, ValueError, OSError) as exc:
        raise DocumentError(f"PDF généré illisible : {exc}") from exc
