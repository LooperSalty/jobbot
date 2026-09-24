"""Job keywords for ATS scoring and CV tailoring.

``extract_keywords`` asks the fast LLM for hard skills / tools / soft skills / languages / title variants with a
required-or-nice flag, keeps only keywords grounded in the offer text, canonicalizes them through the lexicon and
merges them with the deterministic lexicon hits. Without LLM (None or ``LLMError``) the lexicon alone is used.
Weights: required = 1.0, nice-to-have = 0.5, mentioned without cue = 0.8 (0.9 in the "profil" block).
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rapidfuzz import fuzz

from jobbot.llm import LLMClient, LLMError
from jobbot.matching import lexicon
from jobbot.matching._text import display_title, norm, prep
from jobbot.models import JobOffer, KeywordHit

log = logging.getLogger(__name__)

REQUIRED_WEIGHT = 1.0
NICE_WEIGHT = 0.5
DEFAULT_WEIGHT = 0.8
PROFILE_BLOCK_WEIGHT = 0.9
TITLE_WEIGHT = 0.5
LLM_MISSED_CAP = 0.8  # lexicon hits the LLM did not keep are probably secondary
MAX_TITLE_VARIANTS = 3
MAX_DESCRIPTION_CHARS = 7000
MAX_KEYWORD_CHARS = 60
CATEGORIES = ("hard", "tool", "language", "certification", "soft", "title")

REQUIRED_CUES = re.compile(
    r"\b(?:requis|requise|indispensable|obligatoire|exigee?s?|imperati(?:f|ve)|maitris\w*|necessaire|"
    r"solides? (?:connaissances?|competences?|experience)|bonnes? connaissances?|experience significative|"
    r"must|required|mandatory|strong|proficien\w*)\b"
)
NICE_CUES = re.compile(
    r"(?:serait un plus|est un plus|\bun plus\b|\bun atout\b|\batout\b|\bapprecie\w*|\bidealement\b|\bbonus\b|"
    r"\bsouhaite\w*|\bnice to have\b|\bis a plus\b|\bplus appreciated\b|\boptionnel\w*|\bfacultati\w*|\bnotions?\b)"
)
_PROFILE_HEADINGS = re.compile(
    r"^(?:profil recherche|votre profil|le profil|profil|competences requises|competences attendues|competences|"
    r"qualifications|requirements|vous etes|ce que nous recherchons|prerequis|pre requis|savoir etre|savoir faire)\b"
)
_OTHER_HEADINGS = re.compile(
    r"^(?:missions?|vos missions|descriptif du poste|description du poste|le poste|responsabilites|"
    r"qui sommes nous|l'entreprise|a propos|about us|avantages|benefits|ce que nous offrons|informations)\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[!?;]) |(?<=[.]) (?=[A-ZÀ-Ý•\-])| [•·▪] ")  # lines are single-spaced
_CLAUSE_SPLIT = re.compile(r",|\bet\b|\band\b")
_LANGUAGE_REQUIRED = re.compile(r"\b(?:courant|bilingue|fluent|professionnel|b2|c1|c2)\b")


class _LLMKeyword(BaseModel):
    model_config = ConfigDict(extra="ignore")
    keyword: str
    category: str = "hard"
    required: bool = True


class _LLMKeywordList(BaseModel):
    model_config = ConfigDict(extra="ignore")
    keywords: list[_LLMKeyword] = Field(default_factory=list)


class _Mention(NamedTuple):
    name: str
    order: int  # sentence index (0 = title)
    required: bool
    nice: bool
    profile: bool  # inside the "Profil recherché" block


class _Summary(NamedTuple):
    first: int
    required: bool
    nice: bool
    profile: bool
    count: int


_SYSTEM = (
    "Tu es un expert du recrutement tech en France et des ATS. Tu extrais d'une offre d'emploi les mots-clés "
    "qu'un ATS utiliserait pour classer les CV. Tu n'inventes rien."
)
_PROMPT = """Extrais au maximum {n} mots-clés de l'offre ci-dessous.
Règles :
- category : "hard" (langage, framework, méthode, compétence technique), "tool" (outil/logiciel),
  "soft" (savoir-être), "language" (langue), "certification",
  "title" (intitulé du poste et variantes FR/EN, 3 au maximum) ;
- required = true si c'est exigé / indispensable / au cœur du poste, false si c'est « un plus », « apprécié »,
  « idéalement », « souhaité », « notions » ;
- reprends l'orthographe de l'offre, un seul concept par mot-clé (pas de phrase), sans doublon ;
- chaque mot-clé (hors "title") doit figurer dans l'offre ; ignore les avantages, le salaire et la localisation.
Réponds avec {{"keywords": [{{"keyword": "...", "category": "...", "required": true}}]}}.

Intitulé : {title}
Entreprise : {company}
Description :
{description}"""


def extract_keywords(job: JobOffer, llm: LLMClient | None = None, *, max_keywords: int = 30) -> list[KeywordHit]:
    """Weighted keywords of ``job`` (canonicalized, no duplicates, weights in (0, 1])."""
    if max_keywords <= 0:
        return []
    lexicon_hits = lexicon_keywords(job)
    if llm is None:
        return _finalize(lexicon_hits, max_keywords)
    try:
        llm_hits = _llm_keywords(job, llm)
    except (LLMError, ValidationError, ValueError) as exc:
        log.warning("Extraction de mots-clés par LLM impossible (%s) — repli sur le lexique", exc)
        return _finalize(lexicon_hits, max_keywords)
    return _finalize(_merge(llm_hits, lexicon_hits), max_keywords)


# --------------------------------------------------------------------------- deterministic path


def lexicon_keywords(job: JobOffer) -> list[KeywordHit]:
    """Deterministic keywords: lexicon skills / soft skills / languages found in the offer + its core title."""
    allowed = lexicon.find_skills(f"{job.title}\n{job.description or ''}")
    mentions = [
        _Mention(name, 0, True, False, False)
        for name in sorted(lexicon.find_skills(job.title) | lexicon.find_soft_skills(job.title))
    ]
    for order, (segment, in_profile) in enumerate(_segments(job.description or ""), start=1):
        mentions.extend(_segment_mentions(segment, in_profile, order, allowed))
    summary = _summarize(mentions)
    ranked = sorted(summary.items(), key=lambda item: item[1].first)
    hits = [KeywordHit(keyword=name, weight=_weight(s), category=lexicon.skill_category(name)) for name, s in ranked]
    title = display_title(job.title)
    if title and title.lower() not in {h.keyword.lower() for h in hits}:
        hits.append(KeywordHit(keyword=title[:MAX_KEYWORD_CHARS], weight=TITLE_WEIGHT, category="title"))
    return hits


def _segments(description: str) -> list[tuple[str, bool]]:
    """Sentences/lines of the description with a flag telling whether they sit in the "profil" block."""
    out: list[tuple[str, bool]] = []
    in_profile = False
    for line in prep(description).splitlines():
        stripped = " ".join(line.split()).strip(" -*•·▪:")
        if not stripped:
            continue
        head = norm(stripped)
        is_heading = _PROFILE_HEADINGS.match(head) or _OTHER_HEADINGS.match(head)
        if is_heading and (len(head.split()) <= 6 or ":" in head[:30]):
            in_profile = bool(_PROFILE_HEADINGS.match(head))
            if len(head.split()) <= 3:
                continue
        out.extend((s, in_profile) for s in _SENTENCE_SPLIT.split(stripped) if s.strip())
    return out


def _segment_mentions(segment: str, in_profile: bool, order: int, allowed: set[str]) -> list[_Mention]:
    """One mention per keyword found in a sentence, with the required / nice-to-have cues of that sentence."""
    low = norm(segment)
    has_req, has_nice = bool(REQUIRED_CUES.search(low)), bool(NICE_CUES.search(low))
    nice_clauses = [c for c in _CLAUSE_SPLIT.split(low) if NICE_CUES.search(c)] if has_req and has_nice else []
    tech = lexicon.count_skills(segment, lenient=True).keys() & allowed  # context was checked on the full offer
    found = tech | lexicon.find_soft_skills(segment) | lexicon.find_languages(segment)
    language_required = bool(_LANGUAGE_REQUIRED.search(low))
    mentions = []
    for name in sorted(found):
        if has_req and has_nice:  # "Vous maîtrisez React, et idéalement Docker"
            nice = any(name in _names_in(clause) for clause in nice_clauses)
            required = not nice
        else:
            nice, required = has_nice, has_req or (name in lexicon.LANGUAGE_SKILLS and language_required)
        mentions.append(_Mention(name, order, required, nice, in_profile))
    return mentions


def _names_in(text: str) -> set[str]:
    skills = set(lexicon.count_skills(text, lenient=True))
    return skills | lexicon.find_soft_skills(text) | lexicon.find_languages(text)


def _summarize(mentions: list[_Mention]) -> dict[str, _Summary]:
    grouped: dict[str, list[_Mention]] = {}
    for mention in mentions:
        grouped.setdefault(mention.name, []).append(mention)
    return {
        name: _Summary(
            first=min(m.order for m in ms), required=any(m.required for m in ms), nice=any(m.nice for m in ms),
            profile=any(m.profile for m in ms), count=len(ms),
        )
        for name, ms in grouped.items()
    }


def _weight(s: _Summary) -> float:
    if s.required:
        return REQUIRED_WEIGHT
    if s.nice:
        return NICE_WEIGHT
    base = PROFILE_BLOCK_WEIGHT if s.profile else DEFAULT_WEIGHT
    return min(REQUIRED_WEIGHT, base + (0.1 if s.count >= 2 else 0.0))


# --------------------------------------------------------------------------- LLM path


def _llm_keywords(job: JobOffer, llm: LLMClient) -> list[KeywordHit]:
    prompt = _PROMPT.format(
        n=40, title=job.title, company=job.company or "(non précisée)",
        description=(job.description or "")[:MAX_DESCRIPTION_CHARS],
    )
    answer = llm.complete_json(prompt, _LLMKeywordList, system=_SYSTEM, fast=True)
    grounding = _Grounding(job)
    hits: list[KeywordHit] = []
    titles = 0
    for item in answer.keywords:
        hit = _to_hit(item)
        if hit is None:
            continue
        if hit.category == "title":
            titles += 1
            if titles > MAX_TITLE_VARIANTS:
                continue
        elif not grounding.contains(hit.keyword):
            log.debug("Mot-clé LLM ignoré (absent de l'offre) : %s", hit.keyword)
            continue
        hits.append(hit)
    return hits


def _to_hit(item: _LLMKeyword) -> KeywordHit | None:
    text = " ".join(item.keyword.split()).strip(" .,;:-•*\"'")
    category = item.category.strip().lower() if item.category else "hard"
    category = category if category in CATEGORIES else "hard"
    if not text or len(text) > MAX_KEYWORD_CHARS or (category != "title" and len(text.split()) > 6):
        return None
    weight = REQUIRED_WEIGHT if item.required else NICE_WEIGHT
    if category == "title":
        return KeywordHit(keyword=text, weight=TITLE_WEIGHT, category="title")
    canonical = lexicon.canonicalize(text)
    known = lexicon.is_known(canonical)
    final_category = lexicon.skill_category(canonical) if known and category != "certification" else category
    return KeywordHit(keyword=canonical, weight=weight, category=final_category)


class _Grounding:
    """Checks that an LLM keyword really appears in the offer (no hallucinated requirements)."""

    def __init__(self, job: JobOffer):
        text = f"{job.title}\n{job.description or ''}"
        self.norm = norm(text)
        self.names = lexicon.find_skills(text) | lexicon.find_soft_skills(text) | lexicon.find_languages(text)

    def contains(self, keyword: str) -> bool:
        if keyword in self.names:
            return True
        k = norm(keyword)
        if len(k) < 2:
            return False
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", self.norm):
            return True
        return len(k) >= 6 and fuzz.partial_ratio(k, self.norm) >= 90


# --------------------------------------------------------------------------- merge & finalize


def _merge(llm_hits: list[KeywordHit], lexicon_hits: list[KeywordHit]) -> list[KeywordHit]:
    merged: dict[str, KeywordHit] = {}
    for hit in llm_hits:
        key = hit.keyword.lower()
        prev = merged.get(key)
        merged[key] = hit if prev is None else prev.model_copy(update={"weight": max(prev.weight, hit.weight)})
    llm_has_titles = any(h.category == "title" for h in llm_hits)
    for hit in lexicon_hits:
        key = hit.keyword.lower()
        if key in merged or (hit.category == "title" and llm_has_titles):
            continue
        cap = TITLE_WEIGHT if hit.category == "title" else LLM_MISSED_CAP
        merged[key] = hit.model_copy(update={"weight": min(hit.weight, cap)})
    return list(merged.values())


def _finalize(hits: list[KeywordHit], max_keywords: int) -> list[KeywordHit]:
    unique: dict[str, tuple[int, KeywordHit]] = {}
    for position, hit in enumerate(hits):
        key = hit.keyword.strip().lower()
        if not key:
            continue
        weight = min(1.0, max(0.05, float(hit.weight)))
        clean = hit.model_copy(update={"keyword": hit.keyword.strip(), "weight": weight, "found": False})
        if key not in unique or unique[key][1].weight < weight:
            unique[key] = (unique.get(key, (position, clean))[0], clean)
    ranked = sorted(unique.values(), key=lambda p: (-p[1].weight, _category_rank(p[1].category), p[0]))
    return [hit for _, hit in ranked[:max_keywords]]


def _category_rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)
