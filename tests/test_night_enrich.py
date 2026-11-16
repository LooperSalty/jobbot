"""night.enrich — parallel enrichment (one worker per domain), time budget, capped LLM, safe saves."""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import timedelta

import pytest

from jobbot import clock
from jobbot.config import OutreachSettings
from jobbot.llm import CappedLLM, FakeLLM, LLMError
from jobbot.models import ContactEmail
from jobbot.night import enrich
from jobbot.night.budget import Deadline
from jobbot.night.enrich import domain_key, needs_enrichment, run_enrichment, run_parallel, save_enriched

LATER = Deadline(clock.now() + timedelta(hours=1))


def test_parallel_work_never_runs_twice_at_once_for_the_same_domain():
    lock = threading.Lock()
    running: Counter = Counter()
    peak_per_key: Counter = Counter()
    peak_total = [0]

    def work(item: tuple[str, int]) -> str:
        key = item[0]
        with lock:
            running[key] += 1
            peak_per_key[key] = max(peak_per_key[key], running[key])
            peak_total[0] = max(peak_total[0], sum(running.values()))
        time.sleep(0.02)
        with lock:
            running[key] -= 1
        return f"{key}{item[1]}"

    items = [("a.fr", 1), ("a.fr", 2), ("b.fr", 1), ("c.fr", 1), ("a.fr", 3), ("d.fr", 1), ("b.fr", 2)]
    results, not_started = run_parallel(items, work, workers=4, deadline=LATER, key=lambda i: i[0])
    assert sorted(results) == sorted(f"{k}{n}" for k, n in items) and not_started == 0
    assert max(peak_per_key.values()) == 1  # politeness: one worker per domain
    assert peak_total[0] > 1  # but different domains in parallel


def test_host_throttle_spaces_one_host_without_holding_up_the_others(monkeypatch):
    from jobbot import http

    slept: list[float] = []
    monkeypatch.setattr(http.time, "sleep", slept.append)
    throttle = http.HostThrottle(1.0)
    throttle.wait("https://a.example/1")
    throttle.wait("https://b.example/1")  # another host: no wait
    throttle.wait("https://a.example/2")  # same host: waits for its slot
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.0


def test_nothing_starts_after_the_deadline():
    past = Deadline(clock.now() - timedelta(seconds=1))
    results, not_started = run_parallel([1, 2, 3], lambda i: pytest.fail("démarré après l'heure"), workers=2,
                                        deadline=past, key=lambda i: i)
    assert results == [] and not_started == 3


def test_needs_enrichment(make_company):
    cfg, now = OutreachSettings(), clock.now()
    recent = clock.iso(now - timedelta(days=10))
    stale = clock.iso(now - timedelta(days=61))
    assert needs_enrichment(make_company(status="new"), cfg, now)
    assert not needs_enrichment(make_company(status="enriched", raw={"enriched_at": recent}), cfg, now)
    assert needs_enrichment(make_company(status="enriched", raw={"enriched_at": stale}), cfg, now)
    assert needs_enrichment(make_company(status="no_email", raw={"enriched_at": stale}), cfg, now)
    assert not needs_enrichment(make_company(status="no_email", raw={"enriched_at": recent}), cfg, now)
    lux = make_company(status="no_email", raw={"enriched_at": recent, "country": "LU"})
    assert needs_enrichment(lux, OutreachSettings(country="LU"), now)
    assert not needs_enrichment(lux, OutreachSettings(country="FR"), now)
    guess = ContactEmail(email="a.b@x.fr", source="pattern", kind="hr", person_name="A B")
    strict = OutreachSettings(allow_unverified_guesses=False)
    unusable = make_company(status="enriched", contacts=[guess], raw={"enriched_at": recent})
    assert needs_enrichment(unusable, strict, now) and not needs_enrichment(unusable, cfg, now)
    assert not needs_enrichment(make_company(status="contacted"), cfg, now)


def test_domain_key(make_company):
    assert domain_key(make_company(domain=None, website="https://www.exemple.fr/contact")) == "exemple.fr"
    assert domain_key(make_company(name="Exemple SAS", domain=None, website=None)) == "nom:exemple"


def test_capped_llm_forces_the_fast_model_and_stops():
    inner = FakeLLM("résumé")
    capped = CappedLLM(inner, 2)
    assert capped.complete("a") == "résumé" and capped.complete("b", fast=False) == "résumé"
    with pytest.raises(LLMError):
        capped.complete("c")
    assert [fast for _, _, fast in inner.calls] == [True, True]


def test_save_enriched_never_undoes_what_the_day_did(ctx, make_company):
    before = make_company(status="new", contacts=[])
    ctx.repo.upsert_company(before.model_copy(update={"status": "contacted", "raw": {"contacted_at": "2026-09-27"}}))
    after = before.model_copy(update={"status": "enriched", "raw": {"enriched_at": clock.iso(clock.now())},
                                      "contacts": [ContactEmail(email="rh@acme-logiciels.fr", kind="recruitment")]})
    saved = save_enriched(ctx.repo, before, after)
    stored = ctx.repo.get_company(before.id)
    assert saved.status == stored.status == "contacted"
    assert stored.raw["contacted_at"] == "2026-09-27" and stored.contacts


def test_run_enrichment_counts_new_addresses_and_uses_a_capped_llm(ctx, make_company):
    first = make_company(siren="900000001", name="Exemple Un", status="new", contacts=[], domain=None,
                         website=None, naf_code="62.01Z")
    second = make_company(siren="900000002", name="Exemple Deux", status="new", contacts=[], domain=None,
                          website=None, naf_code="62.01Z")
    off = make_company(siren="900000003", name="Exemple Plomberie", status="new", contacts=[], naf_code="43.22A")
    for company in (first, second, off):
        ctx.repo.upsert_company(company)
    seen_llm = []

    def fake_enrich(night_ctx, company):
        seen_llm.append(night_ctx.llm)
        if company.id == second.id:
            return company.model_copy(update={"status": "no_email", "raw": {"enrich_note": "site introuvable"}})
        found = [ContactEmail(email="recrutement@exemple-un.fr", kind="recruitment"),
                 ContactEmail(email="lea.roux@exemple-un.fr", kind="hr", person_name="Léa ROUX")]
        return company.model_copy(update={"status": "enriched", "contacts": found})

    result = run_enrichment(ctx, LATER, enrich=fake_enrich)
    assert (result.processed, result.with_email, result.irrelevant) == (2, 1, 1)
    assert (result.generic_emails, result.nominative_emails) == (1, 1)
    assert ctx.repo.get_company(first.id).status == "enriched"
    assert ctx.repo.get_company(off.id).status == "irrelevant"  # the gate, before any crawl
    assert all(isinstance(llm, CappedLLM) and llm.max_calls == 80 for llm in seen_llm)


def test_enrich_company_keeps_source_urls_and_site_skills(ctx, make_company, monkeypatch):
    from jobbot.outreach import _nominative, domains, emails, people
    from jobbot.outreach.domains import DomainMatch
    from jobbot.outreach.emails import SiteCrawl

    crawl = SiteCrawl(domain="exemple-dev.fr", pages=2, site_text="Nous développons en React, TypeScript et Docker.",
                      contacts=[ContactEmail(email="recrutement@exemple-dev.fr", confidence=0.9, kind="recruitment")],
                      sources={"recrutement@exemple-dev.fr": "https://exemple-dev.fr/carrieres"})
    match = DomainMatch(domain="exemple-dev.fr", confidence=1.0, method="clearbit", verified_by="siren")
    monkeypatch.setattr(domains, "find_domain_verified", lambda company, **kwargs: match)
    monkeypatch.setattr(emails, "crawl_site", lambda domain, client=None, max_pages=8: crawl)
    monkeypatch.setattr(emails, "has_mx", lambda domain: True)
    monkeypatch.setattr(_nominative, "has_mx", lambda domain: True)
    monkeypatch.setattr(people, "_search_titles", lambda *a, **k: [])
    monkeypatch.setattr(people, "_fetch_registry_leaders", lambda siren, client: [])
    company = make_company(name="Exemple Dev", siren="900000301", domain=None, website=None, contacts=[],
                           raw={"leaders_detail": []})
    out = emails.enrich_company(ctx, company)
    assert out.raw["source_urls"] == {"recrutement@exemple-dev.fr": "https://exemple-dev.fr/carrieres"}
    assert {"React", "TypeScript", "Docker"} <= set(out.raw["site_skills"])


def test_purged_companies_are_enriched_with_role_addresses_only(ctx, make_company, monkeypatch):
    from jobbot.outreach import emails

    purged = make_company(siren="900000401", name="Exemple Purge", status="enriched", contacts=[],
                          raw={"nominative_purged_at": clock.iso(clock.now())})
    fresh = make_company(siren="900000402", name="Exemple Neuf", status="new", contacts=[])
    seen: dict[str, bool] = {}
    found = [ContactEmail(email="contact@exemple.fr", kind="generic"),
             ContactEmail(email="lea.roux@exemple.fr", kind="named", person_name="Léa ROUX")]

    def fake_enrich(night_ctx, company):
        seen[company.name] = night_ctx.settings.outreach.allow_nominative
        sources = {c.email: "https://exemple.fr/equipe" for c in found}
        return company.model_copy(update={"contacts": found, "status": "enriched", "leaders": ["Léa ROUX"],
                                          "raw": {**company.raw, "source_urls": sources, "people": [{"n": 1}]}})

    monkeypatch.setattr(emails, "enrich_company", fake_enrich)
    again = emails.enrich_or_note_failure(ctx, purged)
    other = emails.enrich_or_note_failure(ctx, fresh)
    assert seen == {"Exemple Purge": False, "Exemple Neuf": True}
    assert [c.email for c in again.contacts] == ["contact@exemple.fr"] and again.leaders == []
    assert again.raw["source_urls"] == {"contact@exemple.fr": "https://exemple.fr/equipe"}
    assert "people" not in again.raw and again.status == "enriched"
    assert len(other.contacts) == 2


def test_enrichment_queue_puts_hiring_companies_first(ctx, make_company):
    plain = make_company(siren="900000011", name="Sans Signal", status="new", contacts=[])
    hiring = make_company(siren="900000012", name="Recrute", status="new", contacts=[], raw={"hiring_signal": [
        {"title": "Développeur web", "source": "lba", "date": clock.iso(clock.now())}]})
    ctx.repo.upsert_company(plain)
    ctx.repo.upsert_company(hiring)
    queue, _ = enrich.enrichment_queue(ctx)
    assert [c.name for c in queue] == ["Recrute", "Sans Signal"]
