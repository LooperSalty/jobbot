"""LLM access behind one small interface.

Providers:
- ``claude_cli``: headless Claude Code (``claude -p``) — uses the user's Claude subscription, no API key.
- ``anthropic``: Anthropic API (needs ANTHROPIC_API_KEY).
- ``FakeLLM``: deterministic stub for tests.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from jobbot.config import LLMSettings, Secrets

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str: ...

    def complete_json(
        self, prompt: str, schema: type[T], *, system: str | None = None, fast: bool = False
    ) -> T: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """Pull the JSON payload out of a model answer (fenced block, or first {...}/[...] span)."""
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise LLMError(f"Aucun JSON dans la réponse du modèle: {text[:200]!r}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise LLMError(f"JSON tronqué dans la réponse du modèle: {text[:200]!r}")
    return text[start : end + 1]


class _JsonMixin:
    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str:  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, prompt: str, schema: type[T], *, system: str | None = None, fast: bool = False) -> T:
        """Ask for JSON matching ``schema``; one repair attempt on invalid output."""
        schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        full_prompt = (
            f"{prompt}\n\nRéponds UNIQUEMENT avec un objet JSON valide conforme à ce JSON Schema "
            f"(pas de texte autour):\n{schema_txt}"
        )
        answer = self.complete(full_prompt, system=system, fast=fast)
        try:
            return schema.model_validate_json(extract_json(answer))
        except (ValidationError, LLMError, ValueError) as first_error:
            log.warning("LLM JSON invalide, tentative de réparation: %s", first_error)
            repair = (
                f"{full_prompt}\n\nTa réponse précédente était invalide:\n{answer[:4000]}\n\n"
                f"Erreur: {str(first_error)[:1500]}\nRenvoie le JSON corrigé uniquement."
            )
            answer = self.complete(repair, system=system, fast=fast)
            try:
                return schema.model_validate_json(extract_json(answer))
            except (ValidationError, ValueError) as exc:
                raise LLMError(f"Réponse JSON invalide après réparation: {exc}") from exc


class ClaudeCLIClient(_JsonMixin):
    """Runs ``claude -p`` with tools, MCP servers, settings and session persistence disabled."""

    def __init__(self, settings: LLMSettings):
        self.settings = settings
        exe = shutil.which(settings.cli_path)
        if exe is None:
            raise LLMError(
                f"Commande '{settings.cli_path}' introuvable. Installe Claude Code ou passe llm.provider à 'anthropic'."
            )
        self.exe = exe

    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str:
        model = self.settings.fast_model if fast else self.settings.model
        cmd = [
            self.exe, "-p", "--output-format", "json", "--model", model,
            "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "",
        ]
        if system:
            cmd += ["--system-prompt", system]
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=self.settings.timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p a dépassé {self.settings.timeout_s}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"claude -p a échoué (code {proc.returncode}): {proc.stderr.strip()[:500]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Sortie inattendue de claude -p: {proc.stdout[:300]!r}") from exc
        if payload.get("is_error"):
            raise LLMError(f"Erreur Claude: {payload.get('result') or payload.get('subtype')}")
        return str(payload.get("result", "")).strip()


class AnthropicClient(_JsonMixin):
    def __init__(self, settings: LLMSettings, api_key: str):
        import anthropic

        self.settings = settings
        self.client = anthropic.Anthropic(api_key=api_key, timeout=settings.timeout_s, max_retries=3)

    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str:
        import anthropic

        model = self.settings.fast_model if fast else self.settings.model
        try:
            msg = self.client.messages.create(
                model=model,
                max_tokens=8000,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise LLMError(f"API Anthropic: {exc}") from exc
        return "".join(block.text for block in msg.content if block.type == "text").strip()


class FakeLLM(_JsonMixin):
    """Test double. ``responder(prompt, system, fast) -> str``; records every call."""

    def __init__(self, responder: Callable[[str, str | None, bool], str] | list[str] | str = "{}"):
        self.calls: list[tuple[str, str | None, bool]] = []
        self._responder = responder

    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str:
        self.calls.append((prompt, system, fast))
        r = self._responder
        if callable(r):
            return r(prompt, system, fast)
        if isinstance(r, list):
            if not r:
                raise LLMError("FakeLLM: plus de réponses en file")
            return r.pop(0)
        return r


class DisabledLLM(_JsonMixin):
    def complete(self, prompt: str, *, system: str | None = None, fast: bool = False) -> str:
        raise LLMError("LLM désactivé (llm.provider = none)")


def build_llm(settings: LLMSettings, secrets: Secrets) -> LLMClient:
    if settings.provider == "anthropic":
        if not secrets.anthropic_api_key:
            raise LLMError("llm.provider=anthropic mais ANTHROPIC_API_KEY est absent du .env")
        return AnthropicClient(settings, secrets.anthropic_api_key)
    if settings.provider == "claude_cli":
        return ClaudeCLIClient(settings)
    return DisabledLLM()
