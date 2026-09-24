"""Job-alert e-mails → minimal JobOffers (the ToS-friendly discovery channel for HelloWork, LinkedIn...).

Board recognised by sender domain (or an alert-like subject); offer links pulled from the HTML/text bodies
(tracking redirects unwrapped); title/company/location read around each link. Descriptions are filled later
by the owning source's ``fetch_details`` (``raw["needs_details"]``). Pure parsing: no network.
"""

from __future__ import annotations

import base64
import gzip
import html as htmllib
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parseaddr
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

from jobbot import clock
from jobbot.models import ApplyMethod, JobOffer
from jobbot.sources.linkedin import guess_contract_type
from jobbot.textutil import stable_id, strip_accents, truncate

log = logging.getLogger(__name__)

SENDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "hellowork": ("hellowork.com", "hellowork-group.com", "regionsjob.com"),
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com", "indeed.fr"),  # NOT indeedemail.com (recruiter relay = real replies)
    "wttj": ("welcometothejungle.com", "wttj.co"),
    "france_travail": ("francetravail.fr", "francetravail.org", "pole-emploi.fr", "pole-emploi.org"),
    "lba": ("apprentissage.beta.gouv.fr",),
    "apec": ("apec.fr",),
    "free_work": ("free-work.com",),
}
IGNORED_SENDER_LOCALS = ("messages-noreply", "invitations", "messaging-digest-noreply", "security-noreply")
MAX_UNWRAP_DEPTH = 3

_NOT_ALERT_SUBJECT = re.compile(
    r"(^\s*re\s*:|votre candidature|candidature (a ete |bien )?(envoyee|transmise|recue|consultee|vue)|"
    r"your application|application (sent|viewed|received|was)|suite donnee|reponse (negative|positive)|"
    r"\bmessage\b|invitation|a consulte votre profil|viewed your profile|mot de passe|password|"
    r"code de (verification|securite)|verification code|confirme[rz]? (votre|ton) (adresse|e-?mail|compte))"
)
_ALERT_SUBJECT = re.compile(
    r"(alerte|job alert|nouvelles? offres?|nouveaux? (emplois|postes|jobs)|new jobs?|"
    r"offres? (d'emploi )?(qui (pourraient|correspondent)|correspondant|recommandees|pour vous)|"
    r"jobs? (you may|for you|recommended|similaires?)|emplois? (recommandes|pour vous|similaires?))"
)
_GENERIC_TEXT = re.compile(  # CTA / greeting prefixes (whole words) and badge-only lines
    r"^(?:(?:voir|postuler|consulter|decouvrir|en savoir plus|apply|view|see|show|afficher|plus d'infos?|"
    r"je postule|candidater|gerer|manage|modifier|se desabonner|unsubscribe|bonjour|hello|hi|il y a|publiee?|"
    r"soyez (?:parmi|un)|be an early|\d+ (?:candidats?|applicants?))\b|"
    r"(?:nouveau|nouvelle|new|promu|promoted|sponsorise|sponsored|recrutement actif|actively (?:hiring|recruiting)|"
    r"candidature simplifiee|easy apply)\s*!?$)"
)
_CONTRACT_LINE = re.compile(r"^(cdi|cdd|alternance|stage|interim|freelance|apprentissage|temps (plein|partiel))\b")
_EASY_APPLY = re.compile(r"(candidature simplifiee|easy apply)")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
_B64_TOKEN = re.compile(r"^[A-Za-z0-9_\-+/]{24,}={0,2}$")
_SEPARATORS = re.compile(r"\s+[·•|–—]\s+|\s+-\s+")
_SEPARATOR_LINE = re.compile(r"^[-_=*~.·•\s]{3,}$")


@dataclass(frozen=True)
class _Hit:
    source: str
    external_id: str
    url: str
    extra: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    company: str = ""
    location: str = ""
    context: str = ""
    easy_apply: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.external_id


# --------------------------------------------------------------------------- offer URL matchers

LBA_BASE = "https://labonnealternance.apprentissage.beta.gouv.fr"
FREE_WORK_URL = "https://www.free-work.com/fr/tech-it/job-mission/{job_slug}/{id}"
# (source, sender/host domains, path regex with an "id" group (+ extra groups kept in raw), canonical URL)
_PATH_RULES: tuple[tuple[str, tuple[str, ...], re.Pattern[str], str], ...] = (
    ("hellowork", ("hellowork.com",), re.compile(r"/emplois/(?P<id>\d{5,10})\.html"),
     "https://www.hellowork.com/fr-fr/emplois/{id}.html"),
    ("linkedin", ("linkedin.com",), re.compile(r"/jobs/view/(?:[^/]*?-)?(?P<id>\d{6,})(?:/|$)"),
     "https://www.linkedin.com/jobs/view/{id}/"),
    ("wttj", ("welcometothejungle.com",),
     re.compile(r"^/[a-z]{2}/companies/(?P<organization_slug>[^/]+)/jobs/(?P<job_slug>[^/]+)"),
     "https://www.welcometothejungle.com/fr/companies/{organization_slug}/jobs/{job_slug}"),
    ("france_travail", ("francetravail.fr", "pole-emploi.fr"),
     re.compile(r"/offres/recherche/detail/(?P<id>[0-9A-Z]{5,12})", re.I),
     "https://candidat.francetravail.fr/offres/recherche/detail/{id}"),
    ("lba", ("apprentissage.beta.gouv.fr",), re.compile(r"^/emploi/(?P<lba_type>[a-z_]+)/(?P<id>[^/]+)"),
     LBA_BASE + "/emploi/{lba_type}/{id}/offre"),
    ("apec", ("apec.fr",), re.compile(r"/detail-offre/(?P<id>\d{5,}[A-Z]?)"),
     "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{id}"),
    ("free_work", ("free-work.com",), re.compile(r"/tech-it/job-mission/(?P<job_slug>[^/]+)/(?P<id>[^/]+)"),
     FREE_WORK_URL),
    ("free_work", ("free-work.com",), re.compile(r"/tech-it/(?P<job_slug>[^/]+)/job-mission/(?P<id>[^/]+)"),
     FREE_WORK_URL),
)


def _host_is(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _match_path(host: str, path: str) -> _Hit | None:
    for source, domains, pattern, template in _PATH_RULES:
        m = pattern.search(path) if _host_is(host, *domains) else None
        if m:
            groups = {k: v for k, v in m.groupdict().items() if v}
            groups["id"] = groups.get("id") or f"{groups['organization_slug']}/{groups['job_slug']}"
            if source == "france_travail":
                groups["id"] = groups["id"].upper()
            extra = {k: v for k, v in groups.items() if k != "id"}
            if source == "lba":  # sources/lba.py fetch_details reads raw["source"] (sub-type) + raw["id"]
                extra = {"source": groups["lba_type"], "id": groups["id"]}
            return _Hit(source, groups["id"], template.format(**groups), extra)
    return None


def _match_query(host: str, query: dict[str, str]) -> _Hit | None:
    """Offers identified by a query parameter (Indeed jk, LinkedIn currentJobId, old LBA links)."""
    jk = (query.get("jk") or query.get("vjk") or "").lower()
    if _host_is(host, "indeed.com", "indeed.fr") and re.fullmatch(r"[0-9a-f]{16}", jk):
        return _Hit("indeed", jk, f"https://fr.indeed.com/viewjob?jk={jk}", {"jk": jk})
    job_id = query.get("currentJobId", "")
    if _host_is(host, "linkedin.com") and job_id.isdigit():
        return _Hit("linkedin", job_id, f"https://www.linkedin.com/jobs/view/{job_id}/")
    kind, item = query.get("type", ""), query.get("itemId", "")
    if _host_is(host, "apprentissage.beta.gouv.fr") and kind and item:
        return _Hit("lba", item, f"{LBA_BASE}/emploi/{kind}/{item}/offre", {"source": kind, "id": item})
    return None


def _fold(text: str | None) -> str:
    return " ".join(strip_accents(text or "").lower().replace("’", "'").split())


def _decode_b64(token: str) -> str:
    """Text hidden in a base64 (optionally gzip) tracking token, '' when it is not one."""
    if not _B64_TOKEN.match(token):
        return ""
    try:
        data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
    except (ValueError, OSError, EOFError):  # binascii.Error ⊂ ValueError, BadGzipFile ⊂ OSError
        return ""
    text = data.decode("utf-8", errors="ignore")
    return text if "http" in text else ""


def _embedded_urls(url: str) -> list[str]:
    parts = urlsplit(url)
    tokens = [v for _, v in parse_qsl(parts.query)] + [s for s in parts.path.split("/") if s] + [parts.fragment]
    found = [m.group(0) for m in _URL_IN_TEXT.finditer(unquote(unquote(url)), 8)]  # "…?u=https://…", "/L0/https:…"
    for token in tokens:
        found += _URL_IN_TEXT.findall(_decode_b64(token))
    return [u.rstrip(".,;:!?") for u in found]


def unwrap_urls(url: str, depth: int = 0) -> list[str]:
    """``url`` followed by every URL embedded in it (redirect parameters, base64/gzip tracking payloads)."""
    url = htmllib.unescape((url or "").strip())
    found = [url]
    if depth >= MAX_UNWRAP_DEPTH:
        return found
    for inner in _embedded_urls(url):
        for candidate in unwrap_urls(inner, depth + 1):
            if candidate not in found:
                found.append(candidate)
    return found


def _match_single(url: str) -> _Hit | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    return _match_path(host, unquote(parts.path)) or _match_query(host, dict(parse_qsl(parts.query)))


def match_offer_url(url: str) -> _Hit | None:
    """The board offer behind ``url`` (tracking redirects unwrapped), or None."""
    return next((hit for candidate in unwrap_urls(url) if (hit := _match_single(candidate))), None)


# --------------------------------------------------------------------------- recognition


def _sender(from_addr: str) -> tuple[str, str]:
    addr = (parseaddr(from_addr)[1] or from_addr or "").strip().lower()
    local, _, domain = addr.rpartition("@")
    return local, domain


def recognise_board(from_addr: str) -> str | None:
    """Source name of the job board that sent this e-mail (by sender domain), else None."""
    local, domain = _sender(from_addr)
    if not domain or local.startswith(IGNORED_SENDER_LOCALS):
        return None
    return next((source for source, domains in SENDER_DOMAINS.items() if _host_is(domain, *domains)), None)


def is_alert_email(from_addr: str, subject: str) -> bool:
    """Board sender (or alert-like subject, e.g. a forwarded alert) and not a notification/reply."""
    folded = _fold(subject)
    if _NOT_ALERT_SUBJECT.search(folded) or _sender(from_addr)[0].startswith(IGNORED_SENDER_LOCALS):
        return False
    return recognise_board(from_addr) is not None or bool(_ALERT_SUBJECT.search(folded))


# --------------------------------------------------------------------------- title / company extraction


def _is_good(line: str) -> bool:
    """Could be a title / company / location (not a CTA, URL, separator or boilerplate)."""
    folded = _fold(line)
    return (2 <= len(line) <= 150 and any(c.isalpha() for c in line) and "http" not in folded
            and not folded.startswith("www.") and not _GENERIC_TEXT.match(folded))


def _good_lines(node: Tag) -> list[str]:
    lines = (" ".join(line.split()) for line in node.get_text("\n").splitlines())
    return [line for line in lines if _is_good(line)]


def _company_location(lines: list[str]) -> tuple[str, str]:
    """First non-contract line = "Company · Location" (or company, then location on the next line)."""
    useful = [line for line in lines if not _CONTRACT_LINE.match(_fold(line)) and "€" not in line]
    if not useful:
        return "", ""
    parts = [p.strip() for p in _SEPARATORS.split(useful[0]) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    location = useful[1] if len(useful) > 1 and len(useful[1]) <= 60 else ""
    return useful[0], location


def _card_container(start: Tag, key: tuple[str, str], owner: dict[int, tuple[str, str]]) -> Tag:
    """Largest ancestor of ``start`` that holds links to this offer only (the "job card")."""
    card, node = start, start
    while isinstance(node.parent, Tag) and node.parent.name not in ("body", "html", "[document]"):
        parent = node.parent
        keys = {owner.get(id(a)) for a in parent.find_all("a", href=True)} - {None}
        if keys - {key}:
            break
        card = node = parent
    return card


def _following_lines(card: Tag, owner: dict[int, tuple[str, str]], limit: int = 4) -> list[str]:
    """Flat layouts (<a>title</a><p>company</p>…): text after the card, up to the next offer link."""
    lines: list[str] = []
    for text in card.find_all_next(string=True, limit=80):
        if any(parent is card for parent in text.parents):  # next_elements starts inside the card
            continue
        link = text.find_parent("a")
        if link is not None and id(link) in owner:
            break
        line = " ".join(str(text).split())
        if not isinstance(text, Comment) and _is_good(line):
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _logo_alt(card: Tag) -> str:
    for img in card.find_all("img", alt=True):
        alt = " ".join(str(img.get("alt") or "").split())
        if _is_good(alt) and "logo" not in _fold(alt):
            return alt
    return ""


def _describe(hit: _Hit, anchors: list[Tag], owner: dict[int, tuple[str, str]]) -> _Hit:
    card = _card_container(anchors[0], hit.key, owner)
    card_lines = _good_lines(card)
    anchor_lines = [line for a in anchors for line in _good_lines(a)]
    title = next(iter(anchor_lines + card_lines), "")
    after = card_lines[card_lines.index(title) + 1:] if title in card_lines else card_lines
    after = after or _following_lines(card, owner)
    company, location = _company_location(after[:4])
    return replace(
        hit, title=title, company=company or _logo_alt(card), location=location,
        context=" ".join(after[:4]), easy_apply=bool(_EASY_APPLY.search(_fold(card.get_text(" ")))),
    )


def _hits_from_html(body_html: str) -> dict[tuple[str, str], _Hit]:
    soup = BeautifulSoup(body_html, "lxml")
    hits: dict[tuple[str, str], _Hit] = {}
    anchors: dict[tuple[str, str], list[Tag]] = {}
    owner: dict[int, tuple[str, str]] = {}
    for a in soup.find_all("a", href=True):
        hit = match_offer_url(str(a.get("href") or ""))
        if hit is None:
            continue
        hits.setdefault(hit.key, hit)
        anchors.setdefault(hit.key, []).append(a)
        owner[id(a)] = hit.key
    return {key: _describe(hit, anchors[key], owner) for key, hit in hits.items()}


def _describe_from_block(hit: _Hit, block: list[str]) -> _Hit:
    """Plain-text alert: the lines just above the link are title / company / location."""
    good = [line for line in block if _is_good(line)]
    sep = next((i for i in range(len(good) - 1, 0, -1) if _SEPARATORS.search(good[i])), None)
    if sep is not None:
        title, rest = good[sep - 1], good[sep:]
    else:
        tail = good[-3:]
        title, rest = (tail[0], tail[1:]) if tail else ("", [])
    company, location = _company_location(rest)
    easy = any(_EASY_APPLY.search(_fold(line)) for line in block)
    return replace(hit, title=title, company=company, location=location, context=" ".join(rest), easy_apply=easy)


def _hits_from_text(body_text: str) -> dict[tuple[str, str], _Hit]:
    hits: dict[tuple[str, str], _Hit] = {}
    block: list[str] = []
    for raw_line in body_text.splitlines():
        line = " ".join(raw_line.split())
        urls = _URL_IN_TEXT.findall(line)
        if urls:
            prefix = line[: line.find(urls[0])].strip(" :-–")  # "Développeur web : https://…"
            found = (match_offer_url(url.rstrip(".,;:!?")) for url in urls)
            for hit in (h for h in found if h is not None):
                hits.setdefault(hit.key, _describe_from_block(hit, [*block, prefix]))
            block = []
        elif _SEPARATOR_LINE.match(line):
            block = []
        elif line:
            block = [*block, line]
    return hits


def _merge(primary: dict[tuple[str, str], _Hit], extra: dict[tuple[str, str], _Hit]) -> dict[tuple[str, str], _Hit]:
    """HTML hits first; the text body only adds missing offers or fills empty fields."""
    merged = dict(primary)
    for key, hit in extra.items():
        base = merged.setdefault(key, hit)
        fill = {f: getattr(hit, f) for f in ("title", "company", "location", "context", "easy_apply")
                if not getattr(base, f) and getattr(hit, f)}
        merged[key] = replace(base, **fill)
    return merged


# --------------------------------------------------------------------------- public API


def _to_job(hit: _Hit, *, sender: str, subject: str, now: datetime) -> JobOffer:
    easy = hit.source == "linkedin" and hit.easy_apply
    raw: dict[str, Any] = {"from_alert": True, "alert_sender": sender, "alert_subject": truncate(subject, 200),
                           "needs_details": True, **hit.extra, **({"easy_apply": True} if easy else {})}
    return JobOffer(
        id=stable_id(hit.source, hit.external_id), source=hit.source, external_id=hit.external_id,
        title=truncate(hit.title, 200), company=truncate(hit.company, 120), location=truncate(hit.location, 120),
        url=hit.url, apply_method=ApplyMethod.LINKEDIN_EASY_APPLY if easy else ApplyMethod.EXTERNAL,
        contract_type=guess_contract_type(hit.title, hit.context), found_at=now, raw=raw,
    )


def jobs_from_alert_email(from_addr: str, subject: str, body_html: str, body_text: str) -> list[JobOffer]:
    """Minimal offers (id, source, external_id, url, title/company when present) from a job-alert e-mail.
    [] for anything else (replies, application confirmations...). Never raises: bad e-mails are logged."""
    subject = subject or ""
    try:
        if not is_alert_email(from_addr or "", subject):
            return []
        hits = _merge(_hits_from_html(body_html) if (body_html or "").strip() else {},
                      _hits_from_text(body_text or ""))
    except Exception as exc:  # noqa: BLE001 - one bad e-mail must never break the inbox run
        log.warning("Alerte e-mail illisible (« %s ») : %s", truncate(subject, 80), exc)
        return []
    sender, now, jobs = _sender(from_addr or "")[1], clock.now(), []
    for hit in hits.values():
        try:
            jobs.append(_to_job(hit, sender=sender, subject=subject, now=now))
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            log.warning("Offre ignorée dans l'alerte « %s » (%s) : %s", truncate(subject, 80), hit.url, exc)
    log.info("Alerte « %s » (%s) : %d offre(s) extraite(s)", truncate(subject, 80), sender or "?", len(jobs))
    return jobs
