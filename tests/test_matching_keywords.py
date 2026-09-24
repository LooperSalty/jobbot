"""matching.keywords: deterministic lexicon weights, FakeLLM extraction, grounding, merge, fallbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobbot import clock
from jobbot.llm import FakeLLM, LLMError
from jobbot.matching.keywords import extract_keywords, lexicon_keywords
from jobbot.models import JobOffer

FIXTURES = Path(__file__).parent / "fixtures" / "matching"


def _offer(key: str) -> JobOffer:
    data = json.loads((FIXTURES / "offers.json").read_text(encoding="utf-8"))[key]
    return JobOffer(id=key, found_at=clock.now(), **data)


def _by_name(hits):
    return {h.keyword: h for h in hits}


def test_deterministic_keywords_weights_follow_cues():
    hits = _by_name(extract_keywords(_offer("fullstack_alternance")))
    assert hits["TypeScript"].weight == 1.0 and hits["React"].weight == 1.0  # "Maîtrise … indispensable" / title
    assert hits["Docker"].weight == 0.5  # "Docker est un plus"
    assert hits["Anglais"].weight == 0.5 and hits["Anglais"].category == "language"  # "apprécié"
    assert hits["PostgreSQL"].weight == pytest.approx(0.9)  # profil block, no cue
    assert hits["Autonomie"].category == "soft"
    assert hits["Docker"].category == "tool"
    assert any(h.category == "title" for h in hits.values())
    assert all(0 < h.weight <= 1 for h in hits.values())
    assert not any(h.found for h in hits.values())


def test_deterministic_keywords_are_ordered_and_unique():
    hits = extract_keywords(_offer("fullstack_alternance"))
    weights = [h.weight for h in hits]
    assert weights == sorted(weights, reverse=True)
    names = [h.keyword.lower() for h in hits]
    assert len(names) == len(set(names))


def test_mixed_required_and_nice_in_one_sentence(make_job):
    job = make_job(description="Vous maîtrisez React, et idéalement Docker.")
    hits = _by_name(lexicon_keywords(job))
    assert hits["React"].weight == 1.0
    assert hits["Docker"].weight == 0.5


def test_llm_keywords_are_canonicalized_grounded_and_merged(make_job):
    answer = {
        "keywords": [
            {"keyword": "Postgres", "category": "hard", "required": True},
            {"keyword": "ReactJS", "category": "hard", "required": True},
            {"keyword": "Kubernetes", "category": "tool", "required": True},  # not in the offer → dropped
            {"keyword": "tests unitaires", "category": "hard", "required": False},
            {"keyword": "Développeur full stack", "category": "title", "required": True},
            {"keyword": "Full Stack Developer", "category": "title", "required": True},
            {"keyword": "Fullstack engineer", "category": "title", "required": True},
            {"keyword": "Dev JS", "category": "title", "required": True},  # 4th title variant → dropped
            {"keyword": "esprit d'équipe", "category": "weird", "required": False},
            {"keyword": "", "category": "hard", "required": True},
            {"keyword": "une phrase beaucoup trop longue pour être un mot clé utile", "category": "hard"},
        ]
    }
    llm = FakeLLM(json.dumps(answer))
    job = make_job(
        description="Stack : React, Postgres, Docker. Tests unitaires appréciés. Travail en équipe et esprit d'équipe."
    )
    hits = _by_name(extract_keywords(job, llm))
    assert llm.calls and llm.calls[0][2] is True  # fast model
    assert hits["PostgreSQL"].weight == 1.0 and hits["React"].weight == 1.0
    assert "Kubernetes" not in hits
    assert hits["Tests unitaires"].weight == 0.5
    assert hits["Travail en équipe"].category == "soft"
    assert sum(1 for h in hits.values() if h.category == "title") == 3
    assert hits["Docker"].weight <= 0.8  # lexicon hit the LLM did not keep → capped
    assert "Dev JS" not in hits


@pytest.mark.parametrize("responder", ["pas du json", LLMError("indisponible")])
def test_llm_failure_falls_back_to_lexicon(make_job, responder):
    def answer(prompt, system, fast):
        if isinstance(responder, Exception):
            raise responder
        return responder

    job = make_job()
    assert extract_keywords(job, FakeLLM(answer)) == extract_keywords(job, None)


def test_invalid_llm_schema_falls_back(make_job):
    llm = FakeLLM(json.dumps({"keywords": [{"category": "hard"}]}))  # missing "keyword" twice → LLMError
    job = make_job()
    assert extract_keywords(job, llm) == extract_keywords(job)


def test_max_keywords_and_empty_offer(make_job):
    job = make_job()
    assert len(extract_keywords(job, max_keywords=3)) == 3
    assert extract_keywords(job, max_keywords=0) == []
    empty = make_job(title="Poste", description="")
    assert extract_keywords(empty) == [] or all(h.category == "title" for h in extract_keywords(empty))
