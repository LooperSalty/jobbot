"""User preferences: first-run wizard definition + safe edits of config files.

- Edits keep the YAML comments (ruamel round-trip), are validated against the pydantic models before
  being written, and are atomic (temp file + replace, previous version kept as ``.bak``) — see ``config_files``.
- Settings keys go to ``config/settings.yaml``; a key already set in the gitignored
  ``config/settings.local.yaml`` (it wins over settings.yaml) is updated there instead. Reads return the
  effective value (settings.yaml + settings.local.yaml).
- ``SETUP_QUESTIONS`` is the single definition used by ``jobbot setup`` (CLI) and the dashboard
  "Réglages" page, so both always ask the same things and write to the same place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from jobbot.classroom.models import save_course_urls
from jobbot.config import ConfigError, Profile, Settings, settings_data
from jobbot.config_files import ENV_EXAMPLE, ENV_FILE, load_doc, save_doc, settings_target  # noqa: F401
from jobbot.config_files import LOCAL_SETTINGS_PATH as LOCAL_SETTINGS_FILE  # noqa: F401
from jobbot.config_files import PROFILE_PATH as PROFILE_FILE
from jobbot.config_files import SETTINGS_PATH as SETTINGS_FILE  # noqa: F401
from jobbot.config_files import dig as _dig
from jobbot.config_files import plain as _plain
from jobbot.config_files import secret_present, set_secret  # noqa: F401
from jobbot.config_files import yaml_rt as _yaml

LUXEMBOURG = {
    "city": "Luxembourg", "postal_code": "", "radius_km": 25, "latitude": 49.6116, "longitude": 6.1319,
    "country": "LU", "contract_types": ["cdi"],
}
NANCY = {"city": "Nancy", "postal_code": "54000", "radius_km": 25, "latitude": 48.6921, "longitude": 6.1844,
         "country": "FR"}
CONTRACT_CHOICES = ("alternance", "cdi", "cdd", "stage", "freelance", "interim")

Kind = Literal["bool", "int", "float", "str", "choice", "list", "secret"]


# --------------------------------------------------------------------------- files


def _file_for(key: str) -> tuple[Path, list[str]]:
    """'profile.target.remote_ok' → profile file; anything else → settings file."""
    parts = [p for p in key.split(".") if p]
    if not parts:
        raise ConfigError("Clé vide")
    if parts[0] == "profile":
        return PROFILE_FILE, parts[1:]
    if parts[0] == "settings":
        return SETTINGS_FILE, parts[1:]
    return SETTINGS_FILE, parts


def _validate(file: Path, doc: CommentedMap) -> None:
    try:
        (Profile if file == PROFILE_FILE else Settings).model_validate(_plain(doc))
    except Exception as exc:  # pydantic ValidationError → French message
        raise ConfigError(f"Valeur refusée pour {file.as_posix()} :\n{exc}") from exc


# --------------------------------------------------------------------------- get / set


def parse_value(raw: Any) -> Any:
    """'true' → True, '12' → 12, '[a, b]' → ['a', 'b'], '' → None. Non-strings pass through."""
    if not isinstance(raw, str):
        return raw
    if raw.strip() == "":
        return None
    return _plain(_yaml().load(raw))


def get_value(root: Path, key: str) -> Any:
    """Current effective value (file value — settings.local.yaml over settings.yaml —, else model default)."""
    file, parts = _file_for(key)
    if file == PROFILE_FILE:
        model: Any = Profile.model_validate(_plain(load_doc(root / file)))
    else:
        model = Settings.model_validate(settings_data(root))
    value: Any = model
    for part in parts:
        value = value.get(part) if isinstance(value, dict) else getattr(value, part)
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return getattr(value, "value", value) if value.__class__.__module__ != "builtins" else value


def _stage(root: Path, file: Path, changes: list[tuple[list[str], Any]]) -> CommentedMap:
    doc = load_doc(root / file)
    for parts, value in changes:
        parent, leaf = _dig(doc, parts, create=True)
        parent[leaf] = value
    _validate(file, doc)
    return doc


def set_many(root: Path, updates: dict[str, Any], *, parse: bool = True) -> None:
    """Apply several ``key: value`` updates; each file is validated then written once.

    Settings keys go to settings.yaml, or to settings.local.yaml when that file already sets them.
    With ``parse`` (CLI input), strings are YAML-parsed ("12" → 12); if the typed value is refused,
    the raw string is tried instead (e.g. a salary stored as text).
    """
    by_file: dict[Path, list[tuple[list[str], Any, Any]]] = {}
    for key, value in updates.items():
        file, parts = _file_for(key)
        target = settings_target(root, file, parts)
        by_file.setdefault(target, []).append((parts, parse_value(value) if parse else value, value))
    staged: list[tuple[Path, CommentedMap]] = []
    for file, changes in by_file.items():
        try:
            doc = _stage(root, file, [(p, typed) for p, typed, _ in changes])
        except ConfigError:
            if not parse:
                raise
            doc = _stage(root, file, [(p, raw) for p, _, raw in changes])
        staged.append((root / file, doc))
    for path, doc in staged:  # write only when every file validated
        save_doc(path, doc)


def set_value(root: Path, key: str, value: Any) -> Any:
    set_many(root, {key: value})
    return get_value(root, key)


# --------------------------------------------------------------------------- locations


def _locations(doc: CommentedMap) -> CommentedSeq:
    parent, leaf = _dig(doc, ["target", "locations"], create=True)
    if not isinstance(parent.get(leaf), list):
        parent[leaf] = CommentedSeq()
    return parent[leaf]


def _find_location(root: Path, city: str) -> dict | None:
    for loc in get_value(root, "profile.target.locations"):
        if loc["city"].lower() == city.lower():
            return loc
    return None


def set_location(root: Path, city: str, entry: dict | None) -> None:
    """Add/replace (entry) or remove (None) the location named ``city`` in the profile."""
    doc = load_doc(root / PROFILE_FILE)
    locs = _locations(doc)
    idx = next((i for i, loc in enumerate(locs) if str(loc.get("city", "")).lower() == city.lower()), None)
    if entry is None and idx is not None:
        del locs[idx]
    elif entry is not None and idx is not None:
        locs[idx] = CommentedMap({**_plain(locs[idx]), **entry})
    elif entry is not None:
        locs.append(CommentedMap(entry))
    _validate(PROFILE_FILE, doc)
    save_doc(root / PROFILE_FILE, doc)


# --------------------------------------------------------------------------- wizard definition


@dataclass(frozen=True)
class Question:
    key: str  # config key ("limits.linkedin_per_day", "profile.target.remote_ok") or pseudo key ("loc.luxembourg")
    label: str
    kind: Kind
    section: str
    choices: tuple[str, ...] = ()
    help: str = ""
    getter: Callable[[Path], Any] | None = None  # custom read (pseudo keys)
    setter: Callable[[Path, Any], None] | None = None  # custom write (pseudo keys)


def _metz_radius_get(root: Path) -> int:
    loc = _find_location(root, "Metz")
    return int(loc["radius_km"]) if loc else 40


def _metz_radius_set(root: Path, value: Any) -> None:
    set_location(root, "Metz", {"city": "Metz", "postal_code": "57000", "radius_km": int(value),
                                "latitude": 49.1193, "longitude": 6.1757, "country": "FR"})


def _toggle(city: str, entry: dict) -> tuple[Callable[[Path], bool], Callable[[Path, Any], None]]:
    def get(root: Path) -> bool:
        return _find_location(root, city) is not None

    def set_(root: Path, value: Any) -> None:
        set_location(root, city, dict(entry) if bool(value) else None)

    return get, set_


def _lu_contracts_get(root: Path) -> list[str]:
    loc = _find_location(root, "Luxembourg")
    return list(loc.get("contract_types") or ["cdi"]) if loc else ["cdi"]


def _lu_contracts_set(root: Path, value: Any) -> None:
    if _find_location(root, "Luxembourg") is not None:
        set_location(root, "Luxembourg", {"contract_types": list(value or ["cdi"])})


_nancy_get, _nancy_set = _toggle("Nancy", NANCY)
_lux_get, _lux_set = _toggle("Luxembourg", LUXEMBOURG)

SEC_SEARCH = "Recherche"
SEC_AUTO = "Automatisation"
SEC_OUTREACH = "Candidatures spontanées"
SEC_FOLLOWUP = "Relances"
SEC_NOTIFY = "Notifications"
SEC_ACCOUNTS = "Comptes"
SEC_CLASSROOM = "Offres de l'école (Classroom)"

SETUP_QUESTIONS: tuple[Question, ...] = (
    # --- Recherche
    Question("profile.target.contract_types", "Types de contrat recherchés", "list", SEC_SEARCH, CONTRACT_CHOICES),
    Question("loc.metz_radius", "Rayon de recherche autour de Metz (km)", "int", SEC_SEARCH,
             getter=_metz_radius_get, setter=_metz_radius_set),
    Question("loc.nancy", "Inclure Nancy (25 km) ?", "bool", SEC_SEARCH, getter=_nancy_get, setter=_nancy_set),
    Question("loc.luxembourg", "Inclure le Luxembourg ?", "bool", SEC_SEARCH, getter=_lux_get, setter=_lux_set,
             help="Une alternance française au Luxembourg est quasi impossible : CDI conseillé."),
    Question("loc.luxembourg_contracts", "Contrats acceptés au Luxembourg", "list", SEC_SEARCH, CONTRACT_CHOICES,
             getter=_lu_contracts_get, setter=_lu_contracts_set),
    Question("profile.target.remote_ok", "Accepter le télétravail complet (toute la France) ?", "bool", SEC_SEARCH),
    Question("profile.target.willing_to_relocate", "Prêt à déménager ?", "bool", SEC_SEARCH),
    Question("profile.target.relocation_regions", "Régions acceptées si déménagement (vide = toutes)", "list",
             SEC_SEARCH),
    Question("profile.target.max_experience_years", "Années d'expérience maximum exigées par une offre", "int",
             SEC_SEARCH),
    Question("profile.answers.salary_expectation_cdi", "Prétentions CDI en France (brut annuel €)", "str", SEC_SEARCH),
    Question("profile.answers.salary_expectation_cdi_luxembourg", "Prétentions CDI au Luxembourg (brut annuel €)",
             "str", SEC_SEARCH),
    Question("profile.answers.willing_to_relocate", "Réponse aux formulaires : mobilité géographique", "str",
             SEC_SEARCH),
    # --- Automatisation
    Question("mode", "Mode de candidature", "choice", SEC_AUTO, ("review", "auto"),
             help="review : tu valides chaque candidature dans le tableau de bord. auto : envoi automatique."),
    Question("warmup_days", "Jours en mode validation avant de passer en auto", "int", SEC_AUTO),
    Question("thresholds.min_match_score", "Score minimum pour garder une offre (0-100)", "float", SEC_AUTO),
    Question("thresholds.auto_apply_min_score", "Score minimum pour postuler sans validation (0-100)", "float",
             SEC_AUTO),
    Question("limits.linkedin_per_day", "Candidatures LinkedIn max par jour", "int", SEC_AUTO,
             help="Au-delà de ~20/jour le risque de restriction du compte augmente."),
    Question("limits.hellowork_per_day", "Candidatures HelloWork max par jour", "int", SEC_AUTO),
    Question("limits.lba_per_day", "Candidatures La Bonne Alternance max par jour", "int", SEC_AUTO),
    Question("limits.email_applications_per_day", "Candidatures par e-mail (offres) max par jour", "int", SEC_AUTO),
    # --- Candidatures spontanées
    Question("limits.spontaneous_per_day", "Candidatures spontanées max par jour", "int", SEC_OUTREACH,
             help="Seulement si la montée progressive (limits.spontaneous_ramp : départ, pas par semaine, maximum) "
                  "est désactivée."),
    Question("outreach.allow_nominative", "Écrire aux personnes nommées (dirigeants, salariés) ?", "bool",
             SEC_OUTREACH),
    Question("outreach.allow_employee_search", "Chercher les RH / responsables techniques de l'entreprise ?", "bool",
             SEC_OUTREACH),
    Question("outreach.allow_unverified_guesses", "Envoyer à une adresse devinée (prenom.nom@) non vérifiable ?",
             "bool", SEC_OUTREACH,
             help="En cas d'erreur de livraison, le format suivant est essayé ; pause automatique si trop d'erreurs."),
    Question("outreach.max_unverified_per_day", "Adresses devinées max par jour", "int", SEC_OUTREACH),
    # --- Relances
    Question("followup.first_after_days", "Première relance après (jours)", "int", SEC_FOLLOWUP),
    Question("followup.second_after_days", "Deuxième relance après la première (jours)", "int", SEC_FOLLOWUP),
    Question("followup.max_followups", "Nombre maximum de relances", "int", SEC_FOLLOWUP),
    # --- Notifications
    Question("notify_jarvis", "Annonces vocales Jarvis (entretiens, réponses) ?", "bool", SEC_NOTIFY),
    Question("reports.weekly_report", "Rapport hebdomadaire ?", "bool", SEC_NOTIFY),
    Question("reports.weekly_email", "Recevoir aussi le rapport hebdomadaire par e-mail ?", "bool", SEC_NOTIFY),
    Question("reports.interview_prep", "Fiche de préparation automatique quand un entretien arrive ?", "bool",
             SEC_NOTIFY),
    Question("notify_email", "Recevoir par e-mail les notifications importantes (entretien, réponse, reconnexion) ?",
             "bool", SEC_NOTIFY, help="Utile sur un serveur sans écran ; objet « [jobbot] … », "
                                      "notify_email_max_per_day par jour au plus (SMTP requis)."),
    Question("notify_ntfy_topic", "Sujet ntfy pour les notifications sur le téléphone (vide = désactivé)", "str",
             SEC_NOTIFY, help="Application ntfy : abonne-toi au même sujet. Choisis un nom long et difficile à "
                              "deviner (lettres, chiffres, - et _) : quiconque le connaît lit les messages."),
    Question("notify_telegram", "Notifications importantes par Telegram ?", "bool", SEC_NOTIFY,
             help="Bot Telegram : TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env (le déploiement serveur les "
                  "renseigne) ; notify_telegram_all: true pour recevoir aussi les bilans."),
    # --- Comptes (.env)
    Question("SMTP_PASSWORD", "Mot de passe d'application Gmail (16 caractères)", "secret", SEC_ACCOUNTS,
             help="myaccount.google.com/apppasswords (validation en 2 étapes requise)."),
    Question("FRANCE_TRAVAIL_CLIENT_ID", "France Travail — identifiant client", "secret", SEC_ACCOUNTS),
    Question("FRANCE_TRAVAIL_CLIENT_SECRET", "France Travail — clé secrète", "secret", SEC_ACCOUNTS),
    Question("LBA_API_TOKEN", "La Bonne Alternance — jeton API (optionnel)", "secret", SEC_ACCOUNTS),
    Question("HUNTER_API_KEY", "Hunter.io — clé API (optionnel)", "secret", SEC_ACCOUNTS),
    Question("BRAVE_API_KEY", "Brave Search — clé API (optionnel)", "secret", SEC_ACCOUNTS),
    Question("ADZUNA_APP_ID", "Adzuna — Application ID (optionnel, offres d'emploi)", "secret", SEC_ACCOUNTS,
             help="Gratuit : developer.adzuna.com → Register, puis Dashboard (Application ID et Application Key)."),
    Question("ADZUNA_APP_KEY", "Adzuna — Application Key (optionnel)", "secret", SEC_ACCOUNTS),
    Question("JOOBLE_API_KEY", "Jooble — clé API (optionnel, offres d'emploi)", "secret", SEC_ACCOUNTS,
             help="Gratuite sur demande : fr.jooble.org/api/about (clé française ; chaque pays a sa propre clé)."),
    # --- Offres de l'école (Google Classroom)
    Question("classroom.enabled", "Surveiller les offres publiées par l'école sur Google Classroom ?", "bool",
             SEC_CLASSROOM),
    Question("classroom.auto_apply", "Postuler tout de suite aux offres adaptées de l'école (même en rodage) ?",
             "bool", SEC_CLASSROOM, help="Toujours soumis au score, aux quotas, à la liste noire et au contrôle du CV."),
    Question("classroom.poll_minutes", "Vérifier le flux Classroom toutes les … minutes", "int", SEC_CLASSROOM),
    Question("CLASSROOM_COURSE_URLS", "Lien(s) du cours Classroom (séparés par des virgules)", "secret",
             SEC_CLASSROOM, help="https://classroom.google.com/c/… puis « jobbot login classroom ».",
             setter=save_course_urls),
)


def current_answer(root: Path, q: Question) -> Any:
    """Current value shown as the default in the wizard (secrets: True if present, never the value)."""
    if q.kind == "secret":
        return secret_present(root, q.key)
    if q.getter:
        return q.getter(root)
    return get_value(root, q.key)


def apply_answer(root: Path, q: Question, value: Any) -> None:
    """Persist one wizard answer (validated). Empty secret answers keep the existing secret."""
    if q.kind == "secret":
        if value:
            (q.setter or (lambda r, v: set_secret(r, q.key, str(v))))(root, value)
        return
    if q.kind == "choice" and value not in q.choices:
        raise ConfigError(f"{q.label} : choisir parmi {', '.join(q.choices)}")
    if q.kind == "list" and q.choices:
        bad = [v for v in value or [] if v not in q.choices]
        if bad:
            raise ConfigError(f"{q.label} : valeurs inconnues {bad} (possibles : {', '.join(q.choices)})")
    if q.setter:
        q.setter(root, value)
    else:
        set_many(root, {q.key: str(value) if q.kind == "str" else value}, parse=False)


# --------------------------------------------------------------------------- setup state & warm-up


def is_setup_completed(root: Path) -> bool:
    return bool(get_value(root, "setup_completed"))


def mark_setup_completed(root: Path, today: date | None = None) -> None:
    updates: dict[str, Any] = {"setup_completed": True}
    if not get_value(root, "setup_date"):
        updates["setup_date"] = (today or date.today()).isoformat()
    set_many(root, updates)


def effective_mode(settings: Settings, today: date | None = None) -> Literal["auto", "review"]:
    """``review`` while in the warm-up window after setup, else the configured mode."""
    if settings.mode == "review":
        return "review"
    if settings.setup_date and settings.warmup_days > 0:
        start = date.fromisoformat(settings.setup_date)
        if (today or date.today()) < start + timedelta(days=settings.warmup_days):
            return "review"
    return "auto"
