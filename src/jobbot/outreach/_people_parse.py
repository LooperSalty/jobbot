"""Pure parsing helpers for :mod:`jobbot.outreach.people` (no I/O): the :class:`Person` model, person-name
validation, role categories, team/about pages (cards, "Nom – Poste" lines, JSON-LD ``Person`` with a
``jobTitle``) and public search-result titles ("Prénom Nom - Poste - Entreprise | LinkedIn").

Data minimisation (GDPR): only a name, a professional role, where it was found and a professional address.
"""

from __future__ import annotations

import html as htmlmod
import json
import re
from collections.abc import Iterable
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from jobbot.outreach._extract import JSONLD_RE, classify_email, same_organisation
from jobbot.textutil import html_to_text, normalize

Category = Literal["hr", "tech", "leader", "other"]
PersonSource = Literal["registry", "website", "hunter", "search"]

PARTICLES = frozenset({"de", "du", "des", "le", "la", "van", "von", "der", "den", "da", "di", "del", "della", "el",
                       "ben", "bin", "al", "ter", "d'", "l'"})
_NOT_NAME = frozenset(normalize(w) for w in (
    "notre", "nos", "votre", "vos", "équipe", "equipe", "team", "contact", "contactez", "mentions", "légales",
    "services", "service", "nous", "qui", "sommes", "accueil", "projets", "projet", "solutions", "solution",
    "expertise", "expertises", "actualités", "blog", "rejoignez", "rejoindre", "offres", "offre", "agence", "société",
    "entreprise", "direction", "histoire", "valeurs", "clients", "partenaires", "références", "mission", "vision",
    "métiers", "développement", "digital", "web", "conseil", "formation", "support", "recrutement", "carrières",
    "our", "the", "about", "meet", "and", "et", "en", "plus", "savoir", "découvrir", "voir", "lire", "suite", "france",
    "grand", "est", "lorraine", "moselle", "linkedin", "twitter", "facebook", "instagram", "email", "mail",
    "téléphone", "adresse", "horaires", "cookies", "politique", "confidentialité", "conditions", "générales",
    "droits", "réservés", "copyright", "responsable", "directeur", "directrice", "président", "présidente", "gérant",
    "fondateur", "fondatrice", "ceo", "cto", "rh", "manager", "lead", "chef", "chargée", "chargé", "développeur",
    "développeuse", "consultant", "consultante", "ingénieur", "assistant", "assistante", "commercial", "office",
    "news", "newsletter", "menu", "home", "bienvenue", "merci", "logiciel", "logiciels", "application", "cloud",
    "data", "sas", "sarl", "group", "groupe", "technologies", "informatique", "innovation", "studio", "lab",
))
_TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]*")
_HR = r"\b(?:rh|drh|rrh|hr|hrbp)\b|ressources humaines|human resources|recrut|recruit|talent|\bpeople\b"
_TECH = (r"\bcto\b|chief technology|direct(?:eur|rice) (?:technique|informatique|des systemes|r ?d)|"
         r"responsable (?:technique|informatique|(?:du )?developpement|r ?d)|tech(?:nical)? lead|"
         r"lead (?:dev|developer|developpeur|developpeuse|tech)|head of (?:engineering|tech|development|it)|"
         r"vp (?:engineering|tech)|engineering manager|\bdsi\b")
_LEADER = (r"\bpresident|\bgerant|direct(?:eur|rice) general|\bdg\b|\bceo\b|\bpdg\b|fondat|founder|\bdirigeant|"
           r"managing director|\bowner\b|\bassociee?\b")
_OTHER = (r"direct(?:eur|rice)|responsable|manager|\bchef\b|\bchargee?\b|\blead\b|\bhead\b|developpeu|developer|"
          r"engineer|ingenieur|consultant|architecte|\bcoo\b|\bcfo\b|\bcmo\b|assistant|office manager|commercial|"
          r"business|chef de projet|product owner|designer|comptable")
_CATEGORIES: tuple[tuple[Category, re.Pattern[str]], ...] = (
    ("hr", re.compile(_HR)), ("tech", re.compile(_TECH)), ("leader", re.compile(_LEADER)),
    ("other", re.compile(_OTHER)),
)
TEAM_PAGE = re.compile(r"equipe|équipe|team|a-propos|apropos|about|qui-sommes|qui-est|notre-histoire|fondateur|"
                       r"founder|nous-connaitre|presentation|l-agence|societe", re.I)
_LINE = re.compile(r"^(?P<name>[^,–—|:()]{4,40}?)\s*(?:,|–|—| - |\||:|\()\s*(?P<role>[^()]{2,80}?)\)?$")
_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")
_LINKEDIN_SUFFIX = re.compile(r"\s*[|\-–—]\s*linkedin\s*$", re.I)
_AT_COMPANY = re.compile(r"^(?P<role>.*?)\s+(?:chez|at|@|à)\s+(?P<org>.+)$", re.I)
_NAME_CLASS = re.compile(r"name|nom|member|membre|person|team", re.I)
_TESTIMONIAL = re.compile(r"testimonial|temoignage|témoignage|avis|review|quote|citation|client", re.I)
_CARD_TAGS = ("h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "div")
MAX_LINE_CHARS = 130  # "Nom – Poste" lines are short; bounding the input keeps the regexes linear
MAX_TITLE_CHARS = 220


class Person(BaseModel):
    """Someone worth writing to at a company (never private data: name, role, where it was found)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    first: str = ""
    last: str = ""
    role: str = ""
    category: Category = "other"
    source: PersonSource
    source_url: str | None = None  # company web page (never a LinkedIn profile)
    email: str | None = None
    verified: bool = False
    confidence: float = 0.5


# --------------------------------------------------------------------------- names & roles


def looks_like_name(text: str) -> bool:
    """"Julie Martin", "Jean-Pierre DE LA FONTAINE" yes; "Notre équipe", "Acme SAS", "J. Martin" no."""
    text = " ".join((text or "").split())
    tokens = text.split(" ")
    if not 4 <= len(text) <= 40 or not 2 <= len(tokens) <= 4:
        return False
    if not all(_TOKEN.fullmatch(t) for t in tokens):
        return False
    core = [t for i, t in enumerate(tokens) if not (i and t.lower() in PARTICLES)]
    if len(core) < 2 or any(not t[0].isupper() or len(t.strip(".")) < 2 or "." in t for t in core):
        return False
    return not any(normalize(t) in _NOT_NAME for t in tokens)


def split_name(name: str) -> tuple[str, str]:
    """(first, last): "Julie MARTIN" / "MARTIN Julie" / "Jean de La Fontaine" → first name, family name."""
    tokens = " ".join((name or "").split()).split(" ")
    if len(tokens) < 2:
        return (tokens[0] if tokens else ""), ""
    caps = [len(t) > 1 and t.isupper() for t in tokens]
    if caps[0] and not all(caps):
        k = next(i for i, c in enumerate(caps) if not c)
        return " ".join(tokens[k:]), " ".join(tokens[:k])
    if not caps[0] and any(caps):
        k = caps.index(True)
        return " ".join(tokens[:k]), " ".join(tokens[k:])
    return tokens[0], " ".join(tokens[1:])


def display_name(name: str) -> str:
    """Greeting form: "Luc VERDIER" → "Luc Verdier", "JEAN-PIERRE DE LA FONTAINE" → "Jean-Pierre de La Fontaine"."""
    tokens = " ".join((name or "").split()).split(" ")
    out = []
    for i, token in enumerate(tokens):
        if i and token.lower() in PARTICLES and token.lower() not in ("la", "le"):
            out.append(token.lower())
        elif token.isupper() or token.islower():
            out.append(re.sub(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", lambda m: m.group(0).capitalize(), token))
        else:
            out.append(token)
    return " ".join(out)


def role_category(role: str) -> Category | None:
    """hr | tech | leader | other (a job title), None when the text is not a role at all."""
    text = normalize(role)
    return next((cat for cat, pattern in _CATEGORIES if pattern.search(text)), None)


def make_person(name: str, role: str, source: PersonSource, **extra) -> Person | None:
    name = " ".join(name.split())
    category = role_category(role) or ("leader" if source == "registry" else None)
    if category is None or not looks_like_name(name):
        return None
    first, last = split_name(name)
    return Person(name=name, first=first, last=last, role=" ".join(role.split())[:120], category=category,
                  source=source, **extra)


# --------------------------------------------------------------------------- team / about pages


def _jsonld_nodes(node: object) -> Iterable[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _jsonld_nodes(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _jsonld_nodes(value)


def _jsonld_documents(html: str) -> Iterable[object]:
    for block in JSONLD_RE.findall(html or ""):
        try:
            yield json.loads(block.strip())
        except ValueError:
            continue


def _jsonld_person(node: dict) -> tuple[str, str, str | None] | None:
    types = node.get("@type")
    if "Person" not in (types if isinstance(types, list) else [types]):
        return None
    name, role = str(node.get("name") or ""), str(node.get("jobTitle") or "")
    if not name or not role:  # a Person without jobTitle is typically a CMS author: ignored
        return None
    return name, role, str(node.get("email") or "").removeprefix("mailto:").strip().lower() or None


def _jsonld_people(html: str) -> list[tuple[str, str, str | None]]:
    nodes = (node for doc in _jsonld_documents(html) for node in _jsonld_nodes(doc))
    return [person for node in nodes if (person := _jsonld_person(node)) is not None]


def _role_near(element, name: str) -> str | None:
    for sibling in element.find_next_siblings(limit=2):
        text = " ".join(sibling.get_text(" ", strip=True).split())
        if text and len(text) <= 90 and role_category(text):
            return text
    parent = element.parent
    if parent is not None:
        text = " ".join(parent.get_text(" ", strip=True).replace(name, " ", 1).split()).strip(" -–—,|:")
        if text and len(text) <= 90 and role_category(text):
            return text
    return None


def _mailto_near(element) -> str | None:
    """The address of this person's card: a single mailto in a container holding a single name."""
    for container in (element.parent, getattr(element.parent, "parent", None)):
        if container is None:
            continue
        links = container.find_all("a", href=re.compile(r"^mailto:", re.I))
        if len(links) == 1 and len(container.find_all(element.name)) <= 1:
            return str(links[0]["href"])[7:].split("?", 1)[0].strip().lower() or None
    return None


def _in_testimonial(element) -> bool:
    for parent in element.parents:
        classes = " ".join(parent.get("class") or []) if hasattr(parent, "get") else ""
        if parent.name == "blockquote" or _TESTIMONIAL.search(classes):
            return True
    return False


def _foreign_role(role: str, site_domain: str) -> bool:
    """"Directeur général, Transports Durand": the title of someone working for another organisation."""
    label = normalize(site_domain.split(".")[0]) if site_domain else ""
    for part in re.split(r",|\bchez\b|\bat\b|@", role)[1:]:
        part = part.strip(" -–—")
        if part and part[:1].isupper() and role_category(part) is None and label not in normalize(part):
            return True
    return False


def _card_name(element) -> str | None:
    """The person name an element holds (headings/bold anywhere, generic tags only with a name-like class;
    never inside a client testimonial)."""
    if element.name in ("p", "span", "div") and not _NAME_CLASS.search(" ".join(element.get("class") or [])):
        return None
    if _in_testimonial(element):
        return None
    text = element.get_text(" ", strip=True)
    if len(text) > 60:
        return None
    name = " ".join(text.split())
    return name if looks_like_name(name) else None


def _card_people(html: str) -> list[tuple[str, str, str | None]]:
    from bs4 import BeautifulSoup

    out = []
    for element in BeautifulSoup(html or "", "lxml").find_all(_CARD_TAGS):
        name = _card_name(element)
        role = _role_near(element, name) if name else None
        if name and role:
            out.append((name, role, _mailto_near(element)))
    return out


def _line_people(html: str) -> list[tuple[str, str, str | None]]:
    out = []
    for line in html_to_text(html).splitlines():
        line = line.strip()
        match = _LINE.match(line) if len(line) <= MAX_LINE_CHARS else None
        if match and looks_like_name(match.group("name")) and role_category(match.group("role")):
            out.append((match.group("name"), match.group("role"), None))
    return out


def people_from_html(html: str, url: str, site_domain: str, *, team_page: bool | None = None) -> list[Person]:
    """People named with a role on a company page: JSON-LD everywhere, cards/lines on team/about pages only
    (a home page lists client testimonials: "Marc Durand, directeur de X" is not an employee).
    ``team_page`` forces the page type (None = decided from the URL path)."""
    found = _jsonld_people(html)
    if team_page or (team_page is None and TEAM_PAGE.search(urlparse(url or "").path)):
        found += _card_people(html) + _line_people(html)
    out: list[Person] = []
    for name, role, email in found:
        if _foreign_role(role, site_domain):
            continue
        if email and (classify_email(email) is None or not same_organisation(email.split("@")[-1], site_domain)):
            email = None
        person = make_person(name, role, "website", source_url=url, email=email, confidence=0.7)
        if person is not None:
            out.append(person)
    return out


# --------------------------------------------------------------------------- public search results


def _matches_company(text: str, names: list[str]) -> bool:
    return bool(text) and any(
        len(n) >= 3 and fuzz.token_set_ratio(text, n, processor=default_process) >= 85 for n in names
    )


def person_from_title(title: str, company_names: list[str]) -> Person | None:
    """"Julie Martin - Chargée de recrutement - Acme | LinkedIn" → Person (only HR / tech / leaders of *this*
    company; the profile page itself is never fetched)."""
    text = htmlmod.unescape(re.sub(r"<[^>]{0,200}>", "", (title or "")[:MAX_TITLE_CHARS])).strip()
    clean = _LINKEDIN_SUFFIX.sub("", text)
    parts = [p.strip() for p in _TITLE_SPLIT.split(clean) if p.strip()]
    if len(parts) < 2 or not looks_like_name(parts[0]):
        return None
    if len(parts) >= 3:
        role, org = parts[1], " ".join(parts[2:])
    else:
        at = _AT_COMPANY.match(parts[1])
        role, org = (at.group("role"), at.group("org")) if at else ("", parts[1])
    if not _matches_company(org, company_names):
        at = _AT_COMPANY.match(role)
        if not (at and _matches_company(at.group("org"), company_names)):
            return None
        role = at.group("role")
    person = make_person(parts[0], role, "search", confidence=0.6)
    return person if person is not None and person.category != "other" else None


def people_from_titles(titles: Iterable[str], company_names: list[str]) -> list[Person]:
    return [p for p in (person_from_title(t, company_names) for t in titles) if p is not None]
