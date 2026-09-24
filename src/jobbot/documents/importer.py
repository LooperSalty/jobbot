"""Import an existing CV (PDF) into a master-profile dict (config/profile.yaml), LLM first, rules as fallback.

The result always validates as ``jobbot.config.Profile``; the ``target`` block gets sensible defaults for the
user to complete (job titles, locations, contract types).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobbot.config import Profile
from jobbot.documents._import_rules import parse_cv_text
from jobbot.documents.models import DocumentError
from jobbot.documents.render import pdf_text
from jobbot.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

MAX_CV_CHARS = 12000
YAML_HEADER = (
    "# Profil maître — SOURCE DE VÉRITÉ (importé depuis un CV, à relire et compléter).\n"
    "# Les CV / lettres générés ne peuvent contenir QUE des éléments présents ici.\n\n"
)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Identity(_Loose):
    first_name: str
    last_name: str
    email: str = ""
    phone: str = ""
    city: str = ""
    postal_code: str = ""
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    driving_license: str | None = None


class _Experience(_Loose):
    title: str
    company: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    kind: str = "job"
    bullets: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class _Education(_Loose):
    degree: str
    school: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    details: str = ""


class _Project(_Loose):
    name: str
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    url: str | None = None


class _SkillGroup(_Loose):
    category: str
    items: list[str] = Field(default_factory=list)


class _Language(_Loose):
    name: str
    level: str = ""


class ImportedCV(_Loose):
    identity: _Identity
    headline: str = ""
    summary: str = ""
    experiences: list[_Experience] = Field(default_factory=list)
    education: list[_Education] = Field(default_factory=list)
    projects: list[_Project] = Field(default_factory=list)
    skills: list[_SkillGroup] = Field(default_factory=list)
    languages: list[_Language] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    qualities: list[str] = Field(default_factory=list)


SYSTEM = ("Tu extrais fidèlement les informations d'un CV français vers un profil structuré. Tu recopies, tu ne "
          "résumes pas et tu n'inventes rien.")
PROMPT = """Voici le texte extrait d'un CV (PDF). Convertis-le en profil structuré.
Règles :
- recopie les puces d'expérience telles quelles (une puce = un élément de "bullets") ;
- dates au format "AAAA-MM" quand le mois est connu, sinon "AAAA" ; "end" vide si le poste est en cours ;
- "kind" d'une expérience : job, internship (stage), freelance ou seasonal (job saisonnier) ;
- "skills" : groupes de compétences tels qu'ils apparaissent dans le CV (catégorie + éléments) ;
- "skills" d'une expérience : technologies citées dans ses puces ;
- rien d'inventé : un champ inconnu reste vide.

Texte du CV :
{text}"""


def _target_defaults(data: dict[str, Any]) -> dict[str, Any]:
    identity = data.get("identity", {})
    headline = str(data.get("headline") or "")
    title = re.split(r"\s+[—–-]\s+", headline)[0].strip()
    top_skills = [i for g in data.get("skills", []) for i in g.get("items", [])][:8]
    locations = [{"city": identity["city"], "postal_code": identity.get("postal_code", ""), "radius_km": 30}] \
        if identity.get("city") else []
    return {
        "contract_types": ["alternance", "cdi"],
        "job_titles": [title] if title else [],
        "keywords": top_skills,
        "locations": locations,
    }


def to_profile_dict(imported: ImportedCV) -> dict[str, Any]:
    """Profile-shaped dict (validated) from the extracted CV."""
    data = imported.model_dump(exclude_none=True)
    data["headline"] = data.get("headline") or "Développeur"
    data["summary"] = data.get("summary") or ""
    data["target"] = _target_defaults(data)
    try:
        Profile.model_validate(data)
    except ValidationError as exc:
        raise DocumentError(f"Profil importé invalide : {exc}") from exc
    return data


def import_cv(pdf_path: Path, llm: LLMClient) -> dict:
    """Profile YAML dict extracted from an existing CV PDF (LLM, deterministic parsing as fallback)."""
    text = pdf_text(Path(pdf_path))
    if len(text.strip()) < 50:
        raise DocumentError(f"{Path(pdf_path).name} ne contient pas de texte exploitable (PDF scanné ?)")
    if llm is not None:
        try:
            imported = llm.complete_json(PROMPT.format(text=text[:MAX_CV_CHARS]), ImportedCV, system=SYSTEM)
            return to_profile_dict(imported)
        except (LLMError, ValidationError, ValueError, DocumentError) as exc:
            log.warning("Import du CV par LLM impossible (%s) — analyse par règles", exc)
    return to_profile_dict(ImportedCV.model_validate(parse_cv_text(text)))


def write_profile_yaml(data: dict, out: Path) -> Path:
    """Write the profile YAML (UTF-8, key order kept); an existing file is kept as ``<name>.bak``."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        shutil.copy2(out, out.with_name(out.name + ".bak"))
    try:
        Profile.model_validate(data)
    except ValidationError as exc:
        log.warning("Le profil écrit dans %s est incomplet : %s", out, exc)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110, default_flow_style=False)
    out.write_text(YAML_HEADER + body, encoding="utf-8")
    return out
