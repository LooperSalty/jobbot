"""Answer application-form questions: rules → profile → remembered answers → LLM (fast) → fallback.

Truthfulness first: rules only use facts from ``config/profile.yaml``; the LLM is told to answer ``null``
when the profile does not say; attestation / consent checkboxes are never ticked automatically.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

from jobbot.apply import answer_rules as core
from jobbot.apply import answer_rules_extra as extra
from jobbot.apply.answer_rules import Candidate, RuleInput, clean_label, rule_text
from jobbot.apply.coerce import coerce, is_binary
from jobbot.config import Profile
from jobbot.db import Repository
from jobbot.llm import LLMClient, LLMError
from jobbot.models import JobOffer
from jobbot.textutil import normalize, truncate

log = logging.getLogger(__name__)

FieldKind = Literal["text", "number", "textarea", "select", "radio", "checkbox", "typeahead", "file"]
AnswerSource = Literal["rule", "profile", "memory", "llm", "none"]

MEMORY_PREFIX = "answer:"
LLM_MAX_CONFIDENCE = 0.8
MEMORY_CONFIDENCE = 0.95

# Never auto-tick: certifications, consents, terms (research: linkedin.md §5.19).
ATTESTATION_RE = re.compile(
    r"certif|attest|consen|j accepte|accepte les|i agree|agree|acknowledg|reconnais|conditions|terms|politique|"
    r"policy|rgpd|gdpr|donnees personnelles|personal data|privacy|confidentialite"
)
# Company/job-specific open questions: never reuse an answer given to another company.
JOB_SPECIFIC_RE = re.compile(
    r"pourquoi|why|motivation|lettre|cover letter|message|presentez vous|parlez nous de vous|tell us about|"
    r"about yourself|a propos de vous|qu est ce qui vous|what makes you|what interests you|interesse"
)

RULES: tuple[Callable[[RuleInput], Candidate | None], ...] = (
    core.identity, core.years, extra.salary, extra.notice, extra.availability, extra.simple_answers,
    extra.workplace, extra.language, extra.alternance, extra.degree, extra.driving, extra.adult, extra.how_heard,
    extra.current_job, extra.diversity, core.skill_yes_no,
)
_CANONICAL_ANSWER_KEYS = frozenset({
    "work_authorization", "requires_sponsorship", "notice_period", "availability_date",
    "salary_expectation_alternance", "salary_expectation_cdi", "willing_to_relocate", "remote_preference",
    "highest_degree", "english_level", "driving_license", "contract_duration", "how_did_you_hear",
})

SYSTEM_PROMPT = (
    "Tu remplis le formulaire de candidature d'un candidat. Règle absolue : n'invente RIEN. Utilise uniquement "
    "les faits du profil fourni. Si le profil ne permet pas de répondre, renvoie answer = null."
)


class FormQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    kind: FieldKind
    options: list[str] = Field(default_factory=list)
    required: bool = False


class FormAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str | list[str] | None
    confidence: float
    source: AnswerSource


NO_ANSWER = FormAnswer(value=None, confidence=0.0, source="none")


class _LLMAnswer(BaseModel):
    answer: str | None = None
    confidence: float = 0.0


def is_job_specific(label: str) -> bool:
    return bool(JOB_SPECIFIC_RE.search(rule_text(label)))


def memory_key(label: str) -> str:
    return MEMORY_PREFIX + normalize(clean_label(label))


class FormAnswerer:
    """Answers one form question at a time for a given job (or a spontaneous application)."""

    def __init__(self, profile: Profile, llm: LLMClient | None, repo: Repository, job: JobOffer | None = None):
        self.profile = profile
        self.llm = llm
        self.repo = repo
        self.job = job

    # ------------------------------------------------------------------ public API

    def answer(self, q: FormQuestion, *, allow_llm: bool = True) -> FormAnswer:
        text = rule_text(q.label)
        if q.kind == "file" or not text:
            return NO_ANSWER
        if ATTESTATION_RE.search(text) and (q.kind == "checkbox" or is_binary(q.options)):
            return self._from_memory(q) or NO_ANSWER  # consents: only what the user explicitly decided
        found = self._from_freeform(q, text) or self._from_rules(q, text)
        if found is None and not JOB_SPECIFIC_RE.search(text):
            found = self._from_memory(q)
        if found is None and allow_llm and self.llm is not None:
            found = self._from_llm(q)
        return found or self._fallback(q, text) or NO_ANSWER

    def remember(self, label: str, value: str) -> None:
        """Store the user's answer so the same question is answered automatically next time."""
        key = memory_key(label)
        if key == MEMORY_PREFIX or value is None or not str(value).strip():
            log.warning("Réponse non mémorisée (question ou valeur vide) : %r", label)
            return
        self.repo.kv_set(key, str(value).strip())

    # ------------------------------------------------------------------ steps

    def _answer_from(self, q: FormQuestion, cand: Candidate) -> FormAnswer | None:
        value = coerce(q.kind, q.options, cand.values)
        if value is None:
            return None
        return FormAnswer(value=value, confidence=cand.confidence, source=cand.source)  # type: ignore[arg-type]

    def _from_freeform(self, q: FormQuestion, text: str) -> FormAnswer | None:
        """User-written entries of ``profile.answers`` keyed by the question itself."""
        for key, value in self.profile.answers.items():
            if key in _CANONICAL_ANSWER_KEYS or not value:
                continue
            k = rule_text(key.replace("_", " "))
            if k == text or (len(k) >= 12 and fuzz.ratio(k, text) >= 90):
                found = self._answer_from(q, Candidate((value,), 0.95, "profile"))
                if found:
                    return found
        return None

    def _candidates(self, q: FormQuestion, text: str) -> Iterator[Candidate]:
        r = RuleInput(text=text, kind=q.kind, options=tuple(q.options), profile=self.profile, job=self.job)
        for rule in RULES:
            try:
                cand = rule(r)
            except Exception as exc:  # a buggy rule must never break a whole application
                log.warning("Règle %s en erreur pour %r : %s", rule.__name__, q.label, exc)
                continue
            if cand is not None:
                yield cand

    def _from_rules(self, q: FormQuestion, text: str) -> FormAnswer | None:
        return next((a for a in (self._answer_from(q, c) for c in self._candidates(q, text)) if a), None)

    def _from_memory(self, q: FormQuestion) -> FormAnswer | None:
        try:
            raw = self.repo.kv_get(memory_key(q.label))
        except sqlite3.Error as exc:
            log.warning("Lecture des réponses mémorisées impossible : %s", exc)
            return None
        if not raw:
            return None
        return self._answer_from(q, Candidate((raw,), MEMORY_CONFIDENCE, "memory"))

    def _from_llm(self, q: FormQuestion) -> FormAnswer | None:
        try:
            out = self.llm.complete_json(self._prompt(q), _LLMAnswer, system=SYSTEM_PROMPT, fast=True)  # type: ignore[union-attr]
        except LLMError as exc:
            log.warning("LLM indisponible pour la question %r : %s", q.label, exc)
            return None
        except Exception as exc:  # provider bug: fall back, never crash the application
            log.warning("Erreur inattendue du LLM pour %r : %s", q.label, exc)
            return None
        text = (out.answer or "").strip()
        if not text or text.lower() in ("null", "none", "n/a", "inconnu"):
            return None
        confidence = max(0.0, min(float(out.confidence), LLM_MAX_CONFIDENCE))
        return self._answer_from(q, Candidate((text,), confidence, "llm"))

    def _fallback(self, q: FormQuestion, text: str) -> FormAnswer | None:
        """Deterministic short motivation text when the LLM is unavailable."""
        if q.kind != "textarea" or not JOB_SPECIFIC_RE.search(text):
            return None
        job = self.job
        title = job.title if job else "ce poste"
        company = job.company if job and job.company else "votre entreprise"
        summary = self.profile.summary.split(". ")[0].strip().rstrip(".")
        value = (
            f"Votre offre « {title} » chez {company} correspond à mon projet : {self.profile.headline}. "
            f"{summary}. Je serais ravi d'échanger avec vous pour vous présenter mes réalisations."
        )
        return FormAnswer(value=value, confidence=0.6, source="rule")

    # ------------------------------------------------------------------ prompt

    def _prompt(self, q: FormQuestion) -> str:
        p = self.profile
        lines = [
            f"Question du formulaire : « {clean_label(q.label)} »",
            f"Type de champ : {q.kind}{' (obligatoire)' if q.required else ''}",
        ]
        if q.options:
            lines.append("Options (réponds avec UNE option, texte exact) : " + " | ".join(q.options))
        if self.job is not None:
            j = self.job
            lines.append(f"Offre : {j.title} — {j.company} ({j.contract_type.value}, {j.location})")
            lines.append("Extrait de l'offre : " + truncate(j.description, 1500))
        lines += [
            "Profil du candidat (seule source de vérité) :",
            truncate(p.full_text(), 4000),
            "Années d'expérience par compétence : " + ", ".join(f"{k}={v}" for k, v in p.years_experience.items()),
            "Réponses types : " + "; ".join(f"{k}: {v}" for k, v in p.answers.items()),
            f"Ville : {p.identity.city} ; alternance : {p.alternance.model_dump() if p.alternance else 'non'}",
            "Consignes : nombre → entier seul (ex. \"2\") ; choix → exactement une option ; question ouverte → "
            "2 à 5 phrases à la première personne, vouvoiement, dans la langue de la question, 700 caractères max, "
            "sans formule de politesse ; information absente du profil → answer = null. "
            "confidence entre 0 et 1 (ta certitude que la réponse est exacte et vérifiable dans le profil).",
        ]
        return "\n".join(lines)
