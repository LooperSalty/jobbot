"""One Classroom post (+ attachment and linked-page texts) → zero, one or several offers.

LLM (fast model, ``complete_json``) with strict grounding afterwards: an e-mail address or a link that does not
appear in the post / its attachments / its links is dropped, and a company whose name is nowhere in the texts is
blanked (never a letter to an invented company). Reminders, meetings and general information → no offer.
Deterministic fallback on ``LLMError``: offer-like wording + e-mails / URLs found by regex, first line as title.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jobbot.classroom.content import post_urls
from jobbot.classroom.models import ClassroomPost, LinkContent
from jobbot.llm import LLMClient, LLMError
from jobbot.textutil import extract_emails, normalize, normalize_company, truncate

log = logging.getLogger(__name__)

MAX_POST_CHARS = 7000
MAX_PART_CHARS = 4000
MAX_PROMPT_CHARS = 24000
MIN_POST_CHARS = 20
_OFFER_HINT = re.compile(
    r"\b(?:alternance|alternante?s?|apprentie?s?|apprentissage|offres?|postes?|recrute|recrutement|stages?|cdi|"
    r"cdd|candidatures?|postuler|job|emploi)\b"
)
_WEBMAILS = frozenset({"gmail.com", "outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr", "yahoo.fr",
                       "yahoo.com", "orange.fr", "free.fr", "laposte.net", "live.fr", "icloud.com", "wanadoo.fr"})

SYSTEM = ("Tu lis les publications qu'une école publie dans Google Classroom pour relayer des offres d'alternance "
          "ou d'emploi. Tu recopies fidèlement les informations et tu n'inventes jamais rien.")

RULES = """## Consignes
- is_offer_post = true seulement si la publication présente au moins une offre concrète (une entreprise qui
  recrute un alternant, un salarié ou un stagiaire). Rappels, réunions, cours, devoirs, conseils CV, informations
  générales, salons ou forums sans offre précise → is_offer_post = false et offers = [].
- Une publication peut contenir plusieurs offres : une entrée par couple entreprise + poste.
- title : intitulé du poste ; company : entreprise qui recrute (jamais l'école qui relaie, sauf si l'école recrute
  elle-même) ; location : ville (+ code postal s'il est donné) ; contract_type : alternance, apprentissage,
  professionnalisation, cdi, cdd, stage… tel qu'écrit ; target_level : niveau / diplôme visé tel qu'écrit
  (ex. « Bac+3 à Bac+5 », « BTS », « Master »), vide sinon.
- description : missions, profil recherché et informations utiles, reformulées fidèlement (200 mots maximum) ;
  skills : compétences techniques citées.
- contact_email : l'adresse à laquelle ENVOYER la candidature, y compris une adresse de l'école si elle demande
  qu'on lui transmette les CV (apply_via_school = true dans ce cas) ; contact_name : la personne à contacter si
  elle est nommée.
- apply_url : lien pour postuler (offre sur un site d'emploi, formulaire…), recopié exactement ;
  apply_instructions : consignes (pièces à joindre, objet du mail, référence…) ; deadline : date limite telle
  qu'écrite.
- Laisse "" toute information absente. N'invente ni adresse e-mail, ni lien, ni entreprise, ni date."""


class ExtractedOffer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = ""
    company: str = ""
    location: str = ""
    contract_type: str = ""
    target_level: str = ""
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    contact_email: str = ""
    contact_name: str = ""
    apply_url: str = ""
    apply_instructions: str = ""
    apply_via_school: bool = False
    deadline: str = ""

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        """The model often answers null for "unknown": read it as empty."""
        if not isinstance(data, dict):
            return data
        cleaned = {k: v for k, v in data.items() if v is not None}
        skills = cleaned.get("skills")
        if isinstance(skills, str):
            cleaned["skills"] = [s.strip() for s in skills.split(",") if s.strip()]
        return {k: (" ".join(v.split()) if isinstance(v, str) and k != "description" else v)
                for k, v in cleaned.items()}


class PostExtraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    is_offer_post: bool = False
    offers: list[ExtractedOffer] = Field(default_factory=list)


# --------------------------------------------------------------------------- corpus & prompt


def corpus(post: ClassroomPost, contents: list[LinkContent]) -> str:
    """Every text the offers may come from (grounding reference)."""
    parts = [post.text, *(f"{a.title}\n{a.text}" for a in post.attachments),
             *(f"{c.url}\n{c.title}\n{c.text}" for c in contents), *post.links]
    return "\n".join(p for p in parts if p)


def build_prompt(post: ClassroomPost, contents: list[LinkContent]) -> str:
    parts = [f"## Publication ({post.author or 'auteur inconnu'}, {post.date_text or 'date inconnue'})\n"
             + truncate(post.text, MAX_POST_CHARS)]
    if post.links:
        parts.append("## Liens de la publication\n" + "\n".join(post.links[:10]))
    for att in (a for a in post.attachments if a.text):
        parts.append(f"## Pièce jointe : {att.title or att.kind}\n{truncate(att.text, MAX_PART_CHARS)}")
    for content in (c for c in contents if c.text and c.kind != "error"):
        parts.append(f"## Page liée : {content.url}\n{truncate(content.text, MAX_PART_CHARS)}")
    body = truncate("\n\n".join(parts), MAX_PROMPT_CHARS)
    return f"{body}\n\n{RULES}\n\nRéponds avec {{\"is_offer_post\": …, \"offers\": [...]}}."


# --------------------------------------------------------------------------- grounding


def _in(text: str, haystack: str) -> bool:
    return bool(text) and text.lower() in haystack.lower()


def _company_grounded(company: str, folded_corpus: str) -> bool:
    words = [w for w in normalize_company(company).split() if len(w) >= 3]
    return not words or any(w in folded_corpus for w in words)


def ground(extraction: PostExtraction, post: ClassroomPost, contents: list[LinkContent]) -> PostExtraction:
    """Drop what the texts do not contain (invented e-mail, link or company); offers without title and
    company are removed; a non-offer post never carries offers."""
    if not extraction.is_offer_post:
        return PostExtraction()
    text = corpus(post, contents)
    folded = normalize(text)
    urls = [*post_urls(post), *(c.url for c in contents)]
    offers: list[ExtractedOffer] = []
    for offer in extraction.offers:
        update: dict[str, Any] = {}
        if offer.contact_email and not _in(offer.contact_email, text):
            update["contact_email"] = ""
        if offer.apply_url and not (_in(offer.apply_url, text) or offer.apply_url in urls):
            update["apply_url"] = ""
        if offer.company and not _company_grounded(offer.company, folded):
            update["company"] = ""
        fixed = offer.model_copy(update=update) if update else offer
        if fixed.title or fixed.company:
            offers.append(fixed)
    return PostExtraction(is_offer_post=bool(offers), offers=offers)


# --------------------------------------------------------------------------- fallback & entry point


def company_from_email(address: str) -> str:
    """'recrutement@acme-logiciels.fr' → 'Acme Logiciels' (never for webmails)."""
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    if not domain or domain in _WEBMAILS:
        return ""
    label = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part)


def fallback_extraction(post: ClassroomPost, contents: list[LinkContent]) -> PostExtraction:
    """Without the LLM: one offer when the post reads like an offer (first line = title)."""
    if not _OFFER_HINT.search(normalize(post.text)):
        return PostExtraction()
    emails = extract_emails(corpus(post, contents))
    urls = post_urls(post)
    lines = [line.strip(" -•:") for line in post.text.splitlines() if line.strip(" -•:")]
    email = emails[0] if emails else ""
    offer = ExtractedOffer(
        title=truncate(lines[0], 150) if lines else "", company=company_from_email(email),
        description=truncate(post.text, 3000), contact_email=email, apply_url=urls[0] if urls else "",
    )
    return PostExtraction(is_offer_post=True, offers=[offer])


def extract_offers(llm: LLMClient | None, post: ClassroomPost,
                   contents: list[LinkContent]) -> tuple[PostExtraction, bool]:
    """(offers of the post, True when the LLM produced them). Never raises."""
    if len(post.text.strip()) < MIN_POST_CHARS and not any(a.text for a in post.attachments):
        return PostExtraction(), False
    if llm is not None:
        try:
            raw = llm.complete_json(build_prompt(post, contents), PostExtraction, system=SYSTEM, fast=True)
            return ground(raw, post, contents), True
        except (LLMError, ValidationError, ValueError) as exc:
            log.warning("Extraction des offres par LLM impossible (%s) : règles de secours.", exc)
    return ground(fallback_extraction(post, contents), post, contents), False
