"""« jobbot setup » (first-run wizard over ``preferences.SETUP_QUESTIONS``) and « jobbot config show/get/set »."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, InvalidResponse, Prompt
from rich.table import Table
from rich.text import Text

from jobbot.config import ConfigError, load_settings
from jobbot.textutil import normalize

MAX_ATTEMPTS = 5
TRUE_WORDS = frozenset({"o", "oui", "y", "yes", "true", "1", "vrai"})
FALSE_WORDS = frozenset({"n", "non", "no", "false", "0", "faux"})


class FrPrompt(Prompt):
    illegal_choice_message = "[prompt.invalid.choice]Choisis une des valeurs proposées"
    validate_error_message = "[prompt.invalid]Valeur invalide"


class FrIntPrompt(IntPrompt):
    validate_error_message = "[prompt.invalid]Entre un nombre entier"


class FrFloatPrompt(FloatPrompt):
    validate_error_message = "[prompt.invalid]Entre un nombre (ex. 60)"


class OuiNon(Confirm):
    choices = ["o", "n"]
    validate_error_message = "[prompt.invalid]Réponds o (oui) ou n (non)"

    def process_response(self, value: str) -> bool:
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise InvalidResponse(self.validate_error_message)

    def render_default(self, default: Any) -> Text:
        return Text("(o)" if default else "(n)", "prompt.default")


# --------------------------------------------------------------------------- values


def split_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    text = str(raw or "").strip().strip("[]")
    return [part.strip().strip("'\"") for part in text.split(",") if part.strip().strip("'\"")]


def parse_bool(raw: str) -> bool:
    word = str(raw).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(f"Valeur oui/non attendue, reçu « {raw} »")


def parse_for(q: Any, raw: str) -> Any:
    """CLI string → typed answer for question ``q`` (ConfigError in French when invalid)."""
    try:
        if q.kind == "bool":
            return parse_bool(raw)
        if q.kind == "int":
            return int(str(raw).strip())
        if q.kind == "float":
            return float(str(raw).strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{q.label} : nombre attendu, reçu « {raw} »") from exc
    if q.kind == "list":
        return split_list(raw)
    return str(raw).strip()


def format_value(q: Any, value: Any) -> str:
    if q is not None and q.kind == "secret":
        return "✓" if value else "✗"
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "—" if value is None or value == "" else str(value)


def same_value(q: Any, current: Any, value: Any) -> bool:
    if q.kind == "secret":
        return not value
    if q.kind == "list":
        return split_list(current or []) == split_list(value)
    if q.kind == "str":
        return str(value or "") == str(current or "")
    if q.kind == "float" and current is not None and value is not None:
        return float(current) == float(value)
    return value == current


# --------------------------------------------------------------------------- prompting


def _prompt_label(q: Any) -> str:
    text = escape(q.label)
    if q.help:
        text += f" [dim]({escape(q.help)})[/]"
    return text


def ask(console: Console, q: Any, current: Any) -> Any:
    """One rich prompt for question ``q``, current value as the default."""
    label = _prompt_label(q)
    if q.kind == "bool":
        return OuiNon.ask(label, default=bool(current), console=console)
    if q.kind == "int":
        return FrIntPrompt.ask(label, default=int(current) if current is not None else None, console=console)
    if q.kind == "float":
        return FrFloatPrompt.ask(label, default=float(current) if current is not None else None, console=console)
    if q.kind == "choice":
        default = str(current) if current in q.choices else None
        return FrPrompt.ask(label, choices=list(q.choices), default=default, console=console)
    if q.kind == "list":
        hint = f" [dim][{escape(' | '.join(q.choices))}][/]" if q.choices else ""
        raw = FrPrompt.ask(f"{label}{hint} (séparées par des virgules)", default=", ".join(split_list(current or [])),
                           console=console)
        return split_list(raw)
    if q.kind == "secret":
        note = " — déjà renseigné, Entrée pour garder" if current else " — Entrée pour passer"
        return FrPrompt.ask(label + note, password=True, default="", show_default=False, console=console).strip()
    return FrPrompt.ask(label, default="" if current is None else str(current), console=console).strip()


def ask_and_apply(root: Path, console: Console, q: Any) -> bool:
    """Ask until the answer is accepted (max 5 tries). True when a value was written."""
    from jobbot.preferences import apply_answer, current_answer

    for _ in range(MAX_ATTEMPTS):
        current = current_answer(root, q)
        value = ask(console, q, current)
        if same_value(q, current, value):
            return False
        try:
            apply_answer(root, q, value)
            return True
        except ConfigError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
    console.print("[yellow]Trop d'essais : valeur inchangée.[/]")
    return False


def find_sections(section: str | None) -> list[str]:
    from jobbot.preferences import SETUP_QUESTIONS

    names = list(dict.fromkeys(q.section for q in SETUP_QUESTIONS))
    if not section:
        return names
    wanted = normalize(section)
    return [name for name in names if wanted and wanted in normalize(name)]


def run_wizard(root: Path, console: Console, section: str | None = None) -> bool:
    """Interactive setup (all sections, or one). Ends with ``mark_setup_completed``. False when aborted."""
    from jobbot.preferences import SETUP_QUESTIONS, effective_mode, mark_setup_completed

    sections = find_sections(section)
    if not sections:
        console.print(f"[red]Section inconnue : {escape(section or '')}[/] (possibles : "
                      f"{escape(', '.join(find_sections(None)))})")
        return False
    console.print(Panel("Réponds aux questions ; [bold]Entrée[/] garde la valeur actuelle (entre parenthèses).\n"
                        "Tout reste modifiable plus tard : « jobbot setup --section … », « jobbot config set … » "
                        "ou la page Réglages du tableau de bord.", title="Configuration de jobbot"))
    changed = 0
    try:
        for name in sections:
            console.rule(f"[bold]{escape(name)}")
            for q in (q for q in SETUP_QUESTIONS if q.section == name):
                changed += int(ask_and_apply(root, console, q))
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Assistant interrompu : les réponses déjà données sont enregistrées.[/]")
        return False
    mark_setup_completed(root)
    mode = effective_mode(load_settings(root))
    console.print(f"[green]Configuration terminée[/] ({changed} réglage(s) modifié(s)). Mode effectif : "
                  f"[bold]{mode}[/].")
    return True


# --------------------------------------------------------------------------- config show / get / set


def question(key: str) -> Any:
    from jobbot.preferences import SETUP_QUESTIONS

    return next((q for q in SETUP_QUESTIONS if q.key == key), None)


def config_table(root: Path, section: str | None) -> Table:
    from jobbot.preferences import SETUP_QUESTIONS, current_answer

    sections = find_sections(section)
    table = Table(title="Réglages jobbot", title_justify="left")
    for column in ("Section", "Clé", "Réglage", "Valeur"):
        table.add_column(column)
    for q in (q for q in SETUP_QUESTIONS if q.section in sections):
        try:
            value = format_value(q, current_answer(root, q))
        except Exception as exc:  # a broken file must not hide the other values
            value = f"illisible ({exc})"
        table.add_row(q.section, q.key, q.label, value)
    return table


def config_get(root: Path, key: str) -> str:
    from jobbot.preferences import current_answer, get_value, secret_present

    q = question(key)
    if q is not None:
        return format_value(q, current_answer(root, q))
    if key.isupper():
        return "✓" if secret_present(root, key) else "✗"
    try:
        return format_value(None, get_value(root, key))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"Clé inconnue : {key}") from exc


def config_set(root: Path, key: str, raw: str) -> str:
    """Validated write; returns the French confirmation. Secrets are never echoed."""
    from jobbot.preferences import apply_answer, set_secret, set_value

    q = question(key)
    if (q is not None and q.kind == "secret") or key.isupper():
        set_secret(root, key, raw)
        return f"{key} enregistré dans .env (valeur masquée)."
    if q is not None:
        apply_answer(root, q, parse_for(q, raw))
        return f"{q.label} : {config_get(root, key)}"
    try:
        value = set_value(root, key, raw)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ConfigError(f"Clé inconnue : {key}") from exc
    return f"{key} = {format_value(None, value)}"
