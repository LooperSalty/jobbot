"""outreach._patterns — address pattern inference, rendering, kv storage; guess_patterns with a learned pattern."""

from __future__ import annotations

import pytest

from jobbot.outreach._mailcheck import guess_patterns
from jobbot.outreach._patterns import (
    KV_PREFIX,
    apply_pattern,
    infer_domain_pattern,
    infer_from_local,
    infer_pattern,
    is_valid_pattern,
    learned_pattern,
    person_for_local,
    remember_pattern,
    render_local,
)


@pytest.mark.parametrize(("pattern", "expected"), [
    ("{first}.{last}", "jean.dupont"), ("{f}{last}", "jdupont"), ("{f}.{last}", "j.dupont"), ("{first}", "jean"),
    ("{last}.{first}", "dupont.jean"), ("{first}{l}", "jeand"), ("{first}_{last}", "jean_dupont"),
])
def test_render_local(pattern, expected):
    assert render_local(pattern, "Jean", "DUPONT") == expected


def test_render_handles_accents_compounds_and_garbage():
    assert render_local("{first}.{last}", "Élodie Marie", "Müller") == "elodie.muller"
    assert render_local("{first}.{last}", "Jean", "De La Fontaine", dashed=True) == "jean.de-la-fontaine"
    assert render_local("{first}.{last}", "", "Dupont") is None
    assert render_local("{prenom}.{nom}", "Jean", "Dupont") is None  # not a known placeholder
    assert render_local("..", "Jean", "Dupont") is None
    assert apply_pattern("{first}.{last}", "Jean", "De La Fontaine", "acme.fr") == [
        "jean.delafontaine@acme.fr", "jean.de-la-fontaine@acme.fr"]
    assert apply_pattern("{first}", "Jean", "Dupont", "") == []


@pytest.mark.parametrize(("local", "expected"), [
    ("julie.martin", "{first}.{last}"), ("jmartin", "{f}{last}"), ("j.martin", "{f}.{last}"), ("julie", "{first}"),
    ("martin.julie", "{last}.{first}"), ("martinj", "{last}{f}"), ("julie-martin", "{first}-{last}"),
    ("contact", None),
])
def test_infer_pattern_from_known_person(local, expected):
    assert infer_pattern(local, "Julie", "MARTIN") == expected


def test_infer_from_local_and_domain_votes():
    assert infer_from_local("sylvie.martin") == "{first}.{last}"
    assert infer_from_local("sylvie_martin") == "{first}_{last}"
    assert infer_from_local("smartin") is None and infer_from_local("") is None
    # a local confirmed by a known person outweighs an unconfirmed first.last assumption
    assert infer_domain_pattern(["jmartin", "paul.roux"], [("Julie", "Martin")]) == "{f}{last}"
    assert infer_domain_pattern(["paul.roux", "anne.morel"], []) == "{first}.{last}"
    assert infer_domain_pattern(["x", "contactez"], [("Julie", "Martin")]) is None
    assert infer_domain_pattern([], []) is None


def test_person_for_local():
    names = [("Julie", "Martin"), ("Luc", "VERDIER")]
    assert person_for_local("luc.verdier", names) == ("Luc", "VERDIER")
    assert person_for_local("jmartin", names) == ("Julie", "Martin")
    assert person_for_local("contact", names) is None and person_for_local("jm", names) is None


def test_pattern_storage(repo):
    assert learned_pattern(repo, "acme.fr") is None and learned_pattern(None, "acme.fr") is None
    assert remember_pattern(repo, "ACME.fr", "{f}{last}")
    assert repo.kv_get(f"{KV_PREFIX}acme.fr") == "{f}{last}"
    assert learned_pattern(repo, "acme.fr") == "{f}{last}"
    assert not remember_pattern(repo, "acme.fr", "{prenom}") and not remember_pattern(repo, "", "{first}")
    repo.kv_set(f"{KV_PREFIX}bad.fr", "garbage")
    assert learned_pattern(repo, "bad.fr") is None
    assert is_valid_pattern("{first}.{last}") and not is_valid_pattern(None) and not is_valid_pattern("abc")


def test_guess_patterns_puts_learned_pattern_first(repo):
    priors = guess_patterns("Julie", "Martin", "acme.fr")
    assert priors[:3] == ["julie.martin@acme.fr", "j.martin@acme.fr", "jmartin@acme.fr"]
    learned = guess_patterns("Julie", "Martin", "acme.fr", pattern="{last}{f}")
    assert learned[0] == "martinj@acme.fr" and set(priors) <= set(learned)
    remember_pattern(repo, "acme.fr", "{first}")
    assert guess_patterns("Julie", "Martin", "acme.fr", repo=repo)[0] == "julie@acme.fr"
    assert guess_patterns("Julie", "Martin", "other.fr", repo=repo)[0] == "julie.martin@other.fr"
    assert guess_patterns("", "Martin", "acme.fr", pattern="{first}") == []
