"""« Grandes entreprises » focus: ring-by-ring order around the first search area (queue, morning campaign, night
enrichment), large organisations weighed like digital ones, letters aimed at their IT department."""

from __future__ import annotations

import dataclasses

from jobbot.config import LargeCompanies
from jobbot.documents._cv_llm import target_block
from jobbot.models import ContactEmail
from jobbot.night.enrich import enrichment_queue
from jobbot.outreach import spontaneous
from jobbot.outreach.priority import company_priority, focus_ring, ready_queue

METZ = (49.12, 6.18)
RING1 = (49.357, 6.168)  # ≈ 26 km
RING2 = (48.75, 6.0)  # ≈ 43 km
RING3 = (48.52, 6.18)  # ≈ 66 km
OUTSIDE = (48.17, 6.45)  # ≈ 107 km


def _focus(ctx, enabled: bool):
    outreach = ctx.settings.outreach.model_copy(update={"large_companies": LargeCompanies(enabled=enabled)})
    return dataclasses.replace(ctx, settings=ctx.settings.model_copy(update={"outreach": outreach}))


def _enabled(ctx):
    return _focus(ctx, True)


def _disabled(ctx):
    return _focus(ctx, False)  # whatever config/settings.yaml says


def _at(make_company, siren: str, place: tuple[float, float] | None, **kw):
    lat, lon = place if place else (None, None)
    return make_company(siren=siren, name=kw.pop("name", f"ENTREPRISE {siren}"), latitude=lat, longitude=lon, **kw)


def test_focus_ring_counts_rings_from_the_first_search_area(ctx, make_company):
    on = _enabled(ctx).settings.outreach
    rings = [focus_ring(_at(make_company, f"90000060{i}", place), ctx.profile, on)
             for i, place in enumerate((METZ, RING1, RING2, RING3, OUTSIDE, None))]
    assert rings == [0, 1, 2, 3, 4, 4]
    off = _disabled(ctx).settings.outreach
    assert focus_ring(_at(make_company, "900000699", OUTSIDE), ctx.profile, off) == 0


def test_the_large_sites_found_for_the_focus_weigh_like_digital_ones(ctx, make_company):
    bank = _at(make_company, "900000401", METZ, name="BANQUE EXEMPLE", naf_code="64.19Z",
               raw={"categorie_entreprise": "GE", "large_target": True, "site_tranche": "31"}, status="enriched")
    off, _ = company_priority(bank, ctx.profile, ctx.repo, cfg=_disabled(ctx).settings.outreach)
    on, reasons = company_priority(bank, ctx.profile, ctx.repo, cfg=_enabled(ctx).settings.outreach)
    assert on - off == 20
    assert any(r.startswith("Grande structure") and r.endswith("+30") for r in reasons)
    assert any(r.startswith("Cercle 1") for r in reasons)
    shop = _at(make_company, "900000402", METZ, name="MAGASIN EXEMPLE", naf_code="47.11D", status="enriched",
               raw={"categorie_entreprise": "GE"})  # large, but not one of the focus targets (from LBA, offers…)
    assert (company_priority(shop, ctx.profile, ctx.repo, cfg=_disabled(ctx).settings.outreach)[0]
            == company_priority(shop, ctx.profile, ctx.repo, cfg=_enabled(ctx).settings.outreach)[0])


def test_a_company_hiring_developers_never_waits_for_its_ring(ctx, make_company):
    from jobbot import clock

    signal = [{"title": "Développeur web en alternance (H/F)", "date": clock.iso(clock.now()), "source": "lba"}]
    hiring = _at(make_company, "900000403", RING3, raw={"hiring_signal": signal})
    assert focus_ring(hiring, ctx.profile, _enabled(ctx).settings.outreach) == 0


def test_without_coordinates_the_stored_distance_still_places_the_company(ctx, make_company):
    near = _at(make_company, "900000404", None, raw={"distance_km": 3.0})
    assert focus_ring(near, ctx.profile, _enabled(ctx).settings.outreach) == 0


def _two_targets(ctx, make_company):
    """A large site in Metz with a mere contact@, and a digital company 43 km away with a recruitment address."""
    near = _at(make_company, "900000501", METZ, name="BANQUE DE METZ", naf_code="64.19Z", status="enriched",
               raw={"categorie_entreprise": "GE"},
               contacts=[ContactEmail(email="contact@banque-metz.fr", confidence=0.8, kind="generic")])
    far = _at(make_company, "900000502", RING2, name="LOGICIELS LOIN", status="enriched")
    for company in (near, far):
        ctx.repo.upsert_company(company)
    return near, far


def test_the_queue_goes_ring_by_ring_only_when_enabled(ctx, make_company):
    near, far = _two_targets(ctx, make_company)
    assert [r.company.id for r in ready_queue(_disabled(ctx))] == [far.id, near.id]  # best score first
    assert [r.company.id for r in ready_queue(_enabled(ctx))] == [near.id, far.id]  # Metz first
    ctx.repo.upsert_company(far.model_copy(update={"status": "approved"}))  # validated in the dashboard
    assert [r.company.id for r in ready_queue(_enabled(ctx))] == [far.id, near.id]


def test_the_morning_campaign_and_the_night_enrichment_follow_the_rings(ctx, make_company):
    near, far = _two_targets(ctx, make_company)
    focused = _enabled(ctx)
    assert [c.id for c in spontaneous._candidates(focused, [])] == [near.id, far.id]
    new_near = _at(make_company, "900000503", METZ, name="ASSURANCE DE METZ", naf_code="65.12Z", status="new",
                   raw={"categorie_entreprise": "GE"}, contacts=[])
    new_far = _at(make_company, "900000504", RING3, name="EDITEUR LOIN", status="new", contacts=[])
    for company in (new_near, new_far):
        ctx.repo.upsert_company(company)
    queue, _ = enrichment_queue(focused, set_aside=False)
    ids = [c.id for c in queue]
    assert ids.index(new_near.id) < ids.index(new_far.id)


def test_letters_to_a_large_site_aim_at_its_it_department(make_company):
    bank = make_company(name="BANQUE EXEMPLE", naf_code="64.19Z", naf_label=None, raw={"large_target": True})
    block = target_block(None, bank, "Développeur full-stack")
    assert "service informatique" in block
    assert "Activité : code NAF 64.19Z" in block  # rather than « (inconnue) »
    assert "service informatique" not in target_block(None, make_company(), "Développeur full-stack")


def test_the_letter_without_llm_speaks_of_the_it_department(profile, make_company):
    from jobbot.documents._letter_rules import _you, make_letter_input

    bank = make_company(name="BANQUE EXEMPLE", naf_code="64.19Z", raw={"large_target": True})
    text = _you(make_letter_input(profile, None, bank, None))
    assert "service informatique" in text and "correspond au domaine" not in text
