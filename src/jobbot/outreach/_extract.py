"""E-mail extraction primitives (pure functions, no I/O) used by :mod:`jobbot.outreach.emails`.

Handles Cloudflare ``data-cfemail`` protection, ``[at]``/``(dot)``/entity obfuscation, JSON-LD
``email``/``contactPoint`` (Person nodes skipped: CMS authors are false positives), placeholders,
retina image names, npm specifiers, Sentry DSNs, and classifies addresses by role.
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(
    r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63})@((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
    re.I,
)
CF_ATTR_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
JSONLD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_OBFUSCATIONS = (
    (re.compile(r"\s*[\[\(\{<]\s*(?:at|arobase|chez|@)\s*[\]\)\}>]\s*", re.I), "@"),
    (re.compile(r"\s+(?:at|arobase)\s+", re.I), "@"),
    (re.compile(r"\s*[\[\(\{<]\s*(?:dot|point|\.)\s*[\]\)\}>]\s*", re.I), "."),
    (re.compile(r"\s+(?:dot|point)\s+(?=[a-z]{2,24}\b)", re.I), "."),
)
BAD_TLDS = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "css", "js", "json", "map",
                      "woff", "woff2", "ttf", "mp4", "pdf", "php", "html"})
PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.fr", "exemple.fr", "exemple.com", "domain.com", "domaine.fr",
    "domaine.com", "email.com", "email.fr", "mail.com", "votredomaine.fr", "votre-domaine.fr", "yourdomain.com",
    "test.com", "sentry.io", "wixpress.com", "duckduckgo.com", "sentry-next.wixpress.com", "societe.fr",
    "entreprise.fr", "votre-entreprise.fr", "monsite.fr", "site.com",
})
PLACEHOLDER_LOCALS = frozenset({
    "votre", "vous", "your", "you", "nom", "prenom", "prenom.nom", "nom.prenom", "name", "firstname.lastname",
    "email", "mail", "votre.email", "votre-email", "votreemail", "exemple", "example", "john.doe", "jane.doe",
    "jean.dupont", "test", "xxx", "user", "username", "moi", "adresse",
})
HOST_DOMAINS = frozenset({
    "ovh.com", "ovh.net", "ovhcloud.com", "o2switch.fr", "o2switch.net", "ionos.fr", "ionos.com", "1and1.fr",
    "gandi.net", "hostinger.com", "hostinger.fr", "planethoster.info", "infomaniak.com", "infomaniak.ch",
    "wix.com", "squarespace.com", "wordpress.com", "shopify.com", "webflow.io", "amazonaws.com", "godaddy.com",
    "lws.fr", "nuxit.com", "hostpapa.com", "scaleway.com", "online.net", "sentry.io", "wixpress.com",
})
WEBMAIL_DOMAINS = frozenset({
    "gmail.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "neuf.fr", "laposte.net", "outlook.fr",
    "outlook.com", "hotmail.fr", "hotmail.com", "live.fr", "yahoo.fr", "yahoo.com", "icloud.com", "bbox.fr",
    "numericable.fr", "proton.me", "protonmail.com", "gmx.fr", "aol.com",
})
ROLE_APPLY = re.compile(
    r"^(recrutements?|recrute|rh|drh|grh|jobs?|emplois?|carrieres?|careers?|candidatures?|talents?|hr|"
    r"stages?|alternances?|recruitment|recruiting|join|rejoindre)\b"
)
ROLE_GENERIC = re.compile(r"^(contact|bonjour|hello|hi|info|infos|accueil|secretariat|direction|agence|"
                          r"commercial|office|admin|metz|nancy|team|equipe)\b")
ROLE_EXCLUDE = re.compile(
    r"^(dpo|rgpd|privacy|gdpr|donnees|donnees-personnelles|compta|comptabilite|facturation|factures?|invoices?|"
    r"billing|support|sav|technique|abuse|postmaster|hostmaster|webmaster|noreply|no-reply|no\.reply|"
    r"donotreply|do-not-reply|mailer-daemon|newsletter|news|presse|press|marketing|ne-pas-repondre|"
    r"nepasrepondre|unsubscribe|bounce|error-lite)\b"
)
_NAMED = re.compile(r"^[a-z]{2,}[._-][a-z]{2,}$")
ATS_RE = re.compile(
    r"https?://[^\"'\s<>]*(?:jobtoolz\.com|welcometothejungle\.com/(?:fr|en)/companies/|teamtailor\.com|"
    r"jobs\.lever\.co|recruitee\.com|apply\.workable\.com|flatchr\.io|taleez\.com|jobaffinity\.fr|"
    r"digitalrecruiters\.com|smartrecruiters\.com|myworkdayjobs\.com|successfactors\.|softy\.pro|werecruit\.io|"
    r"join\.com|gohire\.io|hellowork\.com/fr-fr/entreprises/)[^\"'\s<>]*",
    re.I,
)
LINK_PRIORITIES = (
    (0, re.compile(r"recrut|carri|career|jobs?\b|emploi|rejoindre|rejoignez|join|talent|postuler", re.I)),
    (1, re.compile(r"contact", re.I)),
    (2, re.compile(r"mention|legal|l[ée]gales", re.I)),
    (3, re.compile(r"equipe|équipe|team|a-propos|qui-sommes|about", re.I)),
)
CAREERS_LINK = LINK_PRIORITIES[0][1]


# --------------------------------------------------------------------------- decoding


def decode_cfemail(hex_str: str) -> str:
    """Decode a Cloudflare email-protection string (first byte = XOR key)."""
    value = (hex_str or "").strip()
    if len(value) < 4 or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ValueError(f"code cfemail invalide : {hex_str!r}")
    key = int(value[:2], 16)
    return "".join(chr(int(value[i:i + 2], 16) ^ key) for i in range(2, len(value), 2))


def deobfuscate(text: str) -> str:
    """Undo common obfuscations: HTML entities, %40, ``[at]``, ``(arobase)``, ``(dot)``, `` point ``."""
    out = unquote(htmlmod.unescape(text or ""))
    for pattern, replacement in _OBFUSCATIONS:
        out = pattern.sub(replacement, out)
    return out


def _plausible(local: str, domain: str) -> bool:
    d = domain.lower()
    return (
        d.rsplit(".", 1)[-1] not in BAD_TLDS
        and not any(d == b or d.endswith("." + b) for b in PLACEHOLDER_DOMAINS)
        and local.lower() not in PLACEHOLDER_LOCALS
        and not re.fullmatch(r"[0-9a-f]{16,}", local, re.I)  # Sentry DSN keys
        and not re.fullmatch(r"\d+x", d.split(".")[0])  # retina assets: logo@2x.<ext>
    )


def _jsonld_walk(node: object, out: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _jsonld_walk(item, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    if "Person" in (types if isinstance(types, list) else [types]):
        return
    email = node.get("email")
    if isinstance(email, str) and email.strip():
        out.append(email.strip().removeprefix("mailto:").lower())
    for value in node.values():
        _jsonld_walk(value, out)


def jsonld_emails(html: str) -> list[str]:
    """``email`` values of every JSON-LD node except ``Person`` (Organization, ContactPoint...)."""
    out: list[str] = []
    for block in JSONLD_RE.findall(html or ""):
        try:
            _jsonld_walk(json.loads(block.strip()), out)
        except ValueError:
            continue
    return out


def extract_all_emails(html: str) -> list[str]:
    """Every plausible address in a page (lower-cased, first-seen order), before any domain filtering."""
    found: dict[str, None] = {}
    for hex_str in CF_ATTR_RE.findall(html or "") + CF_HREF_RE.findall(html or ""):
        try:
            found.setdefault(decode_cfemail(hex_str).strip().lower(), None)
        except ValueError:
            log.debug("cfemail illisible ignoré")
    for email in jsonld_emails(html):
        found.setdefault(email, None)
    for local, domain in EMAIL_RE.findall(deobfuscate(JSONLD_RE.sub(" ", html or ""))):
        local = local.strip(".")
        if _plausible(local, domain):
            found.setdefault(f"{local}@{domain}".lower(), None)
    return [e for e in found if EMAIL_RE.fullmatch(e) and _plausible(*e.split("@", 1))]


def same_organisation(email_domain: str, site_domain: str) -> bool:
    """Same domain, a subdomain, or the same name under another TLD (trustteam.eu ↔ trustteam.fr)."""
    e, s = email_domain.lower().removeprefix("www."), site_domain.lower().removeprefix("www.")
    if e == s or e.endswith("." + s) or s.endswith("." + e):
        return True
    e_parts, s_parts = e.split("."), s.split(".")
    return len(e_parts) >= 2 and len(s_parts) >= 2 and e_parts[-2] == s_parts[-2] and len(e_parts[-2]) >= 4


def scrape_emails(html: str, domain: str) -> list[str]:
    """Addresses of ``html`` belonging to ``domain`` (or its sibling TLDs) or to a webmail provider."""
    out = []
    for email in extract_all_emails(html):
        email_domain = email.split("@", 1)[1]
        if same_organisation(email_domain, domain) or email_domain in WEBMAIL_DOMAINS:
            out.append(email)
    return out


# --------------------------------------------------------------------------- classification


def classify_email(email: str) -> str | None:
    """recruitment | generic | named — None when the address must never be used."""
    if "@" not in email:
        return None
    local, domain = email.lower().split("@", 1)
    if ROLE_EXCLUDE.match(local) or not _plausible(local, domain):
        return None
    if any(domain == h or domain.endswith("." + h) for h in HOST_DOMAINS):
        return None
    if ROLE_APPLY.match(local):
        return "recruitment"
    if ROLE_GENERIC.match(local):
        return "generic"
    if _NAMED.match(local):
        return "named"
    return "generic"


def default_confidence(kind: str) -> float:
    return {"recruitment": 0.9, "hr": 0.85, "generic": 0.8, "leader": 0.7, "named": 0.6}.get(kind, 0.5)


# --------------------------------------------------------------------------- links


def page_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute url, anchor text) of every <a href> in the page."""
    from bs4 import BeautifulSoup

    out = []
    for anchor in BeautifulSoup(html or "", "lxml").find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        out.append((urljoin(base_url, href), anchor.get_text(" ", strip=True)))
    return out


def _same_site(url: str, base_url: str) -> bool:
    a = (urlparse(url).hostname or "").removeprefix("www.")
    b = (urlparse(base_url).hostname or "").removeprefix("www.")
    return bool(a) and a == b


def prioritized_links(links: list[tuple[str, str]], base_url: str) -> list[str]:
    """Same-site links worth crawling for addresses, most useful first (careers, contact, legal, team)."""
    ranked: dict[str, int] = {}
    for url, text in links:
        if not _same_site(url, base_url) or re.search(r"\.(pdf|jpe?g|png|zip|docx?)$", url, re.I):
            continue
        clean = url.split("#", 1)[0]
        path = urlparse(clean).path + " " + text
        for priority, pattern in LINK_PRIORITIES:
            if pattern.search(path):
                ranked[clean] = min(priority, ranked.get(clean, priority))
                break
    return sorted(ranked, key=lambda u: ranked[u])


def ats_links(html: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).rstrip(".,);") for m in ATS_RE.finditer(html or "")))


def careers_link(links: list[tuple[str, str]], base_url: str) -> str | None:
    for url, text in links:
        if _same_site(url, base_url) and CAREERS_LINK.search(urlparse(url).path + " " + text):
            return url.split("#", 1)[0]
    return None


def has_contact_form(html: str) -> bool:
    lowered = (html or "").lower()
    return "<form" in lowered and "<textarea" in lowered and ('type="email"' in lowered or "mail" in lowered)


def describe_source(url: str | None) -> str:
    """Human wording of where an address was found (GDPR art. 14 transparency line)."""
    path = urlparse(url or "").path.lower()
    if re.search(r"mention|legal", path):
        return "les mentions légales de votre site"
    if re.search(r"recrut|carri|career|job|rejoindre|rejoignez", path):
        return "la page Carrières de votre site"
    if "contact" in path:
        return "la page Contact de votre site"
    return "votre site internet"
