"""Enrichment step for named contacts (``settings.outreach``): people → address pattern → named addresses and
best-effort guesses.

1. :func:`jobbot.outreach.people.find_people_detailed` (registry, team pages already crawled, Hunter, search
   result titles) — employee search only when ``allow_employee_search`` and no recruitment address exists.
2. Named addresses seen anywhere get a name/role (``luc.verdier@`` → Luc Verdier, leader) and teach the
   domain's pattern (Hunter's pattern wins), stored in ``kv["email_pattern:<domain>"]``.
3. Top HR / tech people (and the leader of a small company) without an address get a guessed one: the learned
   pattern (1 guess) or the priors (2 guesses). Hunter verifies when a key/budget exists (``valid`` → verified,
   ``invalid`` dropped, ``claimed`` → the person is never written to); otherwise the guess is kept unverified
   only when ``allow_unverified_guesses`` (sending is then capped and paused on bounces by the campaign).
"""

from __future__ import annotations

import logging
from collections import Counter

import httpx
from pydantic import BaseModel, ConfigDict, Field

from jobbot.context import AppContext
from jobbot.models import Company, ContactEmail
from jobbot.outreach._extract import ROLE_APPLY, ROLE_GENERIC, WEBMAIL_DOMAINS, classify_email
from jobbot.outreach._mailcheck import guess_patterns, has_mx, hunter_domain_search, hunter_verify, take_hunter_credit
from jobbot.outreach._patterns import infer_domain_pattern, learned_pattern, person_for_local, remember_pattern
from jobbot.outreach.people import PeopleSearch, Person, find_people_detailed, is_small_company

log = logging.getLogger(__name__)

MAX_GUESSED_PEOPLE = 3
PRIOR_GUESSES = 2  # without a learned pattern: prenom.nom then the next prior (tried after a bounce)
VERIFIED_GUESS_CONFIDENCE = 0.4
LEARNED_GUESS_CONFIDENCE = 0.35
PRIOR_GUESS_CONFIDENCE = 0.3
KIND_BY_CATEGORY = {"hr": "hr", "tech": "tech", "leader": "leader", "other": "named"}
MAX_STORED_PEOPLE = 10


class Nominative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contacts: list[ContactEmail] = Field(default_factory=list)  # input contacts (tagged) + new ones
    sources: dict[str, str] = Field(default_factory=dict)  # email -> where it comes from
    people: list[Person] = Field(default_factory=list)
    pattern: str | None = None
    mail_domain: str = ""

    def raw_update(self) -> dict:
        people = [{"name": p.name, "role": p.role, "category": p.category, "source": p.source}
                  for p in self.people[:MAX_STORED_PEOPLE]]
        return {"people": people, "email_pattern": self.pattern, "mail_domain": self.mail_domain}


# --------------------------------------------------------------------------- helpers


def mail_domain(domain: str, contacts: list[ContactEmail]) -> str:
    """Domain the company's addresses use (can differ from the website: trustteam.eu → @trustteam.fr)."""
    counts = Counter(d for c in contacts if (d := c.email.split("@", 1)[-1]) not in WEBMAIL_DOMAINS)
    return counts.most_common(1)[0][0] if counts else domain


def _role_address(email: str) -> bool:
    local = email.split("@", 1)[0]
    return bool(ROLE_APPLY.match(local) or ROLE_GENERIC.match(local))


def _name_key(name: str | None) -> str:
    return " ".join(sorted((name or "").lower().replace("-", " ").split()))


def _person_of(contact: ContactEmail, people: list[Person]) -> Person | None:
    if _role_address(contact.email):
        return None
    if contact.person_name:
        return next((p for p in people if _name_key(p.name) == _name_key(contact.person_name)), None)
    match = person_for_local(contact.email.split("@", 1)[0], [(p.first, p.last) for p in people if p.first and p.last])
    return next((p for p in people if (p.first, p.last) == match), None) if match else None


def tag_contacts(contacts: list[ContactEmail], people: list[Person]) -> list[ContactEmail]:
    """Give named addresses the person's name and role kind (``luc.verdier@`` → Luc VERDIER, leader)."""
    out = []
    for contact in contacts:
        person = _person_of(contact, people)
        if person is not None:
            kind = contact.kind if contact.kind in ("recruitment", "hr", "tech") else KIND_BY_CATEGORY[person.category]
            contact = contact.model_copy(update={"person_name": contact.person_name or person.name, "kind": kind})
        out.append(contact)
    return out


def _website_contacts(people: list[Person]) -> tuple[list[ContactEmail], dict[str, str]]:
    contacts, sources = [], {}
    for p in people:
        if p.source == "website" and p.email and classify_email(p.email) is not None:
            contacts.append(ContactEmail(email=p.email, confidence=0.7, source="scraped",
                                         kind=KIND_BY_CATEGORY[p.category], person_name=p.name))
            sources[p.email] = p.source_url or "website"
    return contacts, sources


def learn_pattern(ctx: AppContext, domain: str, contacts: list[ContactEmail], people: list[Person],
                  hunter_pattern: str | None) -> str | None:
    """Hunter's pattern, else the one inferred from named addresses of ``domain``, else the one learned before."""
    if hunter_pattern and remember_pattern(ctx.repo, domain, hunter_pattern):
        return hunter_pattern
    named = [c.email.split("@", 1)[0] for c in contacts
             if c.email.endswith(f"@{domain}") and not _role_address(c.email)
             and (c.person_name or classify_email(c.email) == "named")]
    inferred = infer_domain_pattern(named, [(p.first, p.last) for p in people])
    if inferred and remember_pattern(ctx.repo, domain, inferred):
        return inferred
    return learned_pattern(ctx.repo, domain)


# --------------------------------------------------------------------------- guesses


def _guess(person: Person, email: str, *, verified: bool, confidence: float) -> ContactEmail:
    return ContactEmail(email=email, confidence=round(max(0.05, confidence), 2), source="pattern",
                        kind=KIND_BY_CATEGORY[person.category], person_name=person.name, verified=verified)


def _guess_person(ctx: AppContext, person: Person, domain: str, pattern: str | None,
                  client: httpx.Client, verify: bool, excluded: frozenset[str] = frozenset()) -> list[ContactEmail]:
    key = ctx.secrets.hunter_api_key if verify else None
    keep_unverified = ctx.settings.outreach.allow_unverified_guesses
    base = LEARNED_GUESS_CONFIDENCE if pattern else PRIOR_GUESS_CONFIDENCE
    guesses = guess_patterns(person.first, person.last, domain, pattern=pattern)
    if pattern and guesses and guesses[0] in excluded:  # their address under the domain pattern is dead: gone
        return []
    candidates = [e for e in guesses if e not in excluded][: 1 if pattern else PRIOR_GUESSES]
    kept: list[ContactEmail] = []
    for index, email in enumerate(candidates):
        verdict = _verdict(ctx, email, key, client)
        if verdict == "valid":
            return [_guess(person, email, verified=True, confidence=VERIFIED_GUESS_CONFIDENCE)]
        if verdict == "claimed":  # the person asked not to be processed: never write to them
            return []
        key = None if verdict == "error" else key  # Hunter unavailable: stop spending credits
        if verdict != "invalid" and keep_unverified:
            kept.append(_guess(person, email, verified=False, confidence=base - 0.1 * index))
    return kept


def _verdict(ctx: AppContext, email: str, key: str | None, client: httpx.Client) -> str | None:
    """Hunter verdict for a guess (None when no key or no credit left this month)."""
    if not key or not take_hunter_credit(ctx.repo, "verify"):
        return None
    return hunter_verify(email, key, client=client)


def guessed_contacts(ctx: AppContext, company: Company, people: list[Person], domain: str, pattern: str | None,
                     known: list[ContactEmail], client: httpx.Client,
                     invalid: frozenset[str] = frozenset()) -> tuple[list[ContactEmail], dict[str, str]]:
    """Guessed addresses for the best HR / tech people (and the leader of a small company) with no address,
    never one Hunter reported invalid. Hunter credits are spent on verification only when no recruitment
    address exists (guesses are then the main target; otherwise they are only a fallback after a bounce)."""
    cfg = ctx.settings.outreach
    verify = bool(ctx.secrets.hunter_api_key) and not any(c.kind == "recruitment" for c in known)
    if not cfg.allow_nominative or not (cfg.allow_unverified_guesses or verify):
        return [], {}
    small = is_small_company(company)
    known_names = {_name_key(c.person_name) for c in known if c.person_name}
    targets = [
        p for p in people
        if p.first and p.last and not p.email and _name_key(p.name) not in known_names
        and (p.category in ("hr", "tech") or (p.category == "leader" and small))
    ][:MAX_GUESSED_PEOPLE]
    if not targets or not has_mx(domain):
        return [], {}
    contacts: list[ContactEmail] = []
    sources: dict[str, str] = {}
    for person in targets:
        for contact in _guess_person(ctx, person, domain, pattern, client, verify, invalid):
            contacts.append(contact)
            sources[contact.email] = f"pattern:{person.source}"
    return contacts, sources


# --------------------------------------------------------------------------- entry point


def _search_people(ctx: AppContext, company: Company, domain: str, contacts: list[ContactEmail],
                   pages: list[tuple[str, str]], client: httpx.Client) -> PeopleSearch:
    cfg = ctx.settings.outreach
    employee = cfg.allow_employee_search
    has_recruitment = any(c.kind == "recruitment" for c in contacts)
    # employee search: Hunter also looks for HR / tech people; otherwise only for a role address (v1 behaviour)
    wanted = not has_recruitment if employee else not any(c.kind in ("recruitment", "hr", "generic") for c in contacts)
    use_hunter = bool(ctx.secrets.hunter_api_key) and wanted and take_hunter_credit(ctx.repo, "search")
    return find_people_detailed(
        company, client=client, secrets=ctx.secrets, domain=domain, pages=pages if employee else [],
        preferred_roles=cfg.preferred_roles, use_hunter=use_hunter, use_search=employee and not has_recruitment,
    )


def _generic_only(ctx: AppContext, domain: str, contacts: list[ContactEmail], client: httpx.Client) -> Nominative:
    """``allow_nominative`` off: Hunter only for a role address when the site gave none (v1 behaviour)."""
    if any(c.kind in ("recruitment", "hr", "generic") for c in contacts) or not ctx.secrets.hunter_api_key:
        return Nominative(contacts=contacts, mail_domain=domain)
    if not take_hunter_credit(ctx.repo, "search"):
        return Nominative(contacts=contacts, mail_domain=domain)
    hunter = hunter_domain_search(domain, ctx.secrets.hunter_api_key, client=client)
    return Nominative(contacts=[*contacts, *hunter], sources={c.email: "hunter" for c in hunter}, mail_domain=domain)


def nominative_contacts(ctx: AppContext, company: Company, domain: str, contacts: list[ContactEmail],
                        pages: list[tuple[str, str]], client: httpx.Client) -> Nominative:
    """``contacts`` (from the website) tagged with people, plus people's addresses and guesses."""
    if not ctx.settings.outreach.allow_nominative:
        return _generic_only(ctx, domain, contacts, client)
    found = _search_people(ctx, company, domain, contacts, pages, client)
    site_contacts, sources = _website_contacts(found.people)
    sources.update({c.email: "hunter" for c in found.contacts})
    tagged = tag_contacts([*contacts, *found.contacts, *site_contacts], found.people)
    mail_dom = mail_domain(domain, tagged)
    pattern = learn_pattern(ctx, mail_dom, tagged, found.people, found.pattern)
    guesses, guess_sources = guessed_contacts(ctx, company, found.people, mail_dom, pattern, tagged, client,
                                              frozenset(found.invalid))
    if guesses:
        log.info("%s : %d adresse(s) nominative(s) devinée(s) (%s)", company.name, len(guesses), pattern or "a priori")
    return Nominative(contacts=[*tagged, *guesses], sources={**sources, **guess_sources}, people=found.people,
                      pattern=pattern, mail_domain=mail_dom)
