"""Who receives a spontaneous application, under ``settings.outreach``.

Order of preference: generic recruitment address (``recrutement@``, ``rh@``) > named HR > named tech manager >
leader (small companies only) > ``contact@`` > other named people (> leaders of larger companies). Inside a tier,
a verified/published address beats a guessed one, then the higher confidence wins.

Guardrails:
- ``allow_nominative`` off → only role addresses (never a person's address);
- an unverified guess (``source="pattern"``, not verified) needs ``allow_unverified_guesses``, is capped per day
  (``max_unverified_per_day``, counted in ``action_log`` as ``spontaneous/unverified_guess``) and is paused while
  the recent bounce rate of spontaneous e-mails exceeds ``max_bounce_rate``;
- never a blacklisted address/domain, never an address already written to;
- after a bounce the company can be retried with the next best address while its attempts stay below
  ``max_attempts_per_company``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jobbot import clock
from jobbot.config import OutreachSettings
from jobbot.context import AppContext
from jobbot.db import Repository
from jobbot.models import Application, ApplicationStatus, Company, ContactEmail
from jobbot.outreach._extract import ROLE_APPLY, ROLE_EXCLUDE, ROLE_GENERIC, classify_email
from jobbot.outreach.people import is_small_company
from jobbot.ramp import is_spontaneous
from jobbot.textutil import normalize_company

CHANNEL = "spontaneous"
UNVERIFIED_ACTION = "unverified_guess"
RECENT_DAYS = 60
BOUNCE_WINDOW_DAYS = 30
TIER = {"recruitment": 0, "hr": 1, "tech": 2, "leader": 3, "generic": 4, "named": 6}
LARGE_LEADER_TIER = 5


class Pick(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contact: ContactEmail | None = None
    reason: str = ""
    deferred: bool = False  # only a guessed address remains and it is blocked today (quota / bounce pause)


class History(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["fresh", "retry", "blocked"]
    reason: str = ""
    bounced: Application | None = None  # latest bounced application when state == "retry"
    attempts: int = 0


# --------------------------------------------------------------------------- contact classification


def is_role_address(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return bool(ROLE_APPLY.match(local) or ROLE_GENERIC.match(local))


def is_guess(contact: ContactEmail) -> bool:
    return contact.source == "pattern" and not contact.verified


def is_nominative(contact: ContactEmail) -> bool:
    """A person's own address (personal data): named, guessed or attached to a person's name."""
    if is_role_address(contact.email):
        return False
    return (bool(contact.person_name) or contact.source == "pattern" or contact.kind in ("hr", "tech", "leader", "named")
            or classify_email(contact.email) == "named")


def usable(contact: ContactEmail, cfg: OutreachSettings) -> bool:
    """Static policy (no quota): may this address ever be used with these settings?"""
    if ROLE_EXCLUDE.match(contact.email.split("@", 1)[0].lower()):
        return False
    if is_nominative(contact) and not cfg.allow_nominative:
        return False
    return not is_guess(contact) or cfg.allow_unverified_guesses


def contact_tier(contact: ContactEmail, *, small_company: bool = True) -> int:
    if contact.kind == "leader" and not small_company:
        return LARGE_LEADER_TIER
    return TIER.get(contact.kind, TIER["named"])


def sort_contacts(contacts: list[ContactEmail], *, small_company: bool = True) -> list[ContactEmail]:
    return sorted(contacts, key=lambda c: (contact_tier(c, small_company=small_company), is_guess(c),
                                           -c.confidence, c.email))


# --------------------------------------------------------------------------- guess guardrails


def bounce_stats(apps: list[Application], days: int = BOUNCE_WINDOW_DAYS) -> tuple[int, int]:
    """(bounced, sent) spontaneous e-mails over the last ``days`` days."""
    since = clock.now() - timedelta(days=days)
    recent = [a for a in apps if is_spontaneous(a) and clock.parse_dt(a.applied_at) >= since]
    return sum(a.status == ApplicationStatus.BOUNCED for a in recent), len(recent)


def guesses_paused(apps: list[Application], cfg: OutreachSettings) -> str | None:
    """Reason why guessed addresses are paused (recent bounce rate above ``max_bounce_rate``), else None."""
    bounced, sent = bounce_stats(apps)
    if sent and bounced / sent > cfg.max_bounce_rate:
        return (f"adresses devinées en pause : {bounced} rebond(s) sur {sent} envoi(s) récents "
                f"(> {cfg.max_bounce_rate:.0%})")
    return None


def unverified_left(repo: Repository, cfg: OutreachSettings, simulated: int = 0) -> int:
    used = repo.count_actions_today(CHANNEL, UNVERIFIED_ACTION)
    return max(0, cfg.max_unverified_per_day - used - simulated)


def record_unverified(repo: Repository, email: str) -> None:
    repo.log_action(CHANNEL, UNVERIFIED_ACTION, True, email)


# --------------------------------------------------------------------------- picking


def _already_used(ctx: AppContext, contact: ContactEmail) -> bool:
    domain = contact.email.split("@", 1)[-1]
    return ctx.repo.is_blacklisted(contact.email, domain) or bool(ctx.repo.find_applications_by_email(contact.email))


def pick_contact(ctx: AppContext, company: Company, *, paused: str | None = None, simulated: int = 0) -> Pick:
    """Best address for ``company`` right now (``simulated`` = guesses already "sent" by a dry run)."""
    cfg = ctx.settings.outreach
    blocked: str | None = None
    for contact in sort_contacts(list(company.contacts), small_company=is_small_company(company)):
        if not usable(contact, cfg) or _already_used(ctx, contact):
            continue
        if is_guess(contact):
            reason = paused or (None if unverified_left(ctx.repo, cfg, simulated) > 0 else
                                f"quota d'adresses devinées atteint ({cfg.max_unverified_per_day}/jour)")
            if reason:
                blocked = blocked or reason
                continue
        return Pick(contact=contact)
    if blocked:
        return Pick(reason=blocked, deferred=True)
    return Pick(reason="aucune adresse utilisable")


# --------------------------------------------------------------------------- history & bounce retry


def company_applications(apps: list[Application], company: Company) -> list[Application]:
    key = normalize_company(company.name)
    return [a for a in apps if (a.company_id and a.company_id == company.id)
            or (key and normalize_company(a.company_name) == key)]


def company_history(apps: list[Application], company: Company, cfg: OutreachSettings,
                    recent_days: int = RECENT_DAYS) -> History:
    """fresh (never applied recently) | retry (every recent application bounced, attempts left) | blocked."""
    mine = company_applications(apps, company)
    since = clock.now() - timedelta(days=recent_days)
    recent = [a for a in mine if clock.parse_dt(a.applied_at) >= since]
    if not recent:
        return History(state="fresh")
    if any(a.status != ApplicationStatus.BOUNCED for a in recent):
        return History(state="blocked", reason=f"candidature déjà envoyée il y a moins de {recent_days} jours")
    attempts = sum(1 for a in mine if is_spontaneous(a))
    if attempts >= cfg.max_attempts_per_company:
        return History(state="blocked", attempts=attempts,
                       reason=f"{attempts} adresse(s) déjà en échec (maximum {cfg.max_attempts_per_company})")
    latest = max(recent, key=lambda a: clock.parse_dt(a.applied_at))
    return History(state="retry", bounced=latest, attempts=attempts)


def bounced_companies(ctx: AppContext, apps: list[Application]) -> list[Company]:
    """Contacted companies whose latest application bounced, with attempts and a fresh address left."""
    cfg = ctx.settings.outreach
    ids = dict.fromkeys(a.company_id for a in apps if a.status == ApplicationStatus.BOUNCED and a.company_id)
    out = []
    for company_id in ids:
        company = ctx.repo.get_company(company_id)
        if company is None or company.status != "contacted":
            continue
        if company_history(apps, company, cfg).state == "retry" and pick_contact(ctx, company).contact is not None:
            out.append(company)
    return out
