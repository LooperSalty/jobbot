"""Safe edits of the configuration files (used by ``preferences``).

- YAML round-trip (ruamel): comments kept, atomic write (temp file + replace), previous version kept as ``.bak``.
- Which settings file a key is written to: ``config/settings.yaml``, unless the key is already set in the
  gitignored ``config/settings.local.yaml`` (machine-specific overrides, e.g. on a server): writing it to
  ``settings.yaml`` would have no effect since the local file wins, so it is updated there instead.
- ``.env`` secrets: created from ``.env.example``, values never logged.
"""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from jobbot.config import LOCAL_SETTINGS_FILE, SETTINGS_FILE, ConfigError

SETTINGS_PATH = Path(SETTINGS_FILE)
LOCAL_SETTINGS_PATH = Path(LOCAL_SETTINGS_FILE)
PROFILE_PATH = Path("config/profile.yaml")
ENV_FILE = Path(".env")
ENV_EXAMPLE = Path(".env.example")


# --------------------------------------------------------------------------- yaml round-trip


def yaml_rt() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def plain(node: Any) -> Any:
    """ruamel containers/scalars → plain Python (for pydantic validation)."""
    if isinstance(node, dict):
        return {str(k): plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [plain(v) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    return str(node)


def load_doc(path: Path) -> CommentedMap:
    if not path.exists():
        return CommentedMap()
    doc = yaml_rt().load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, CommentedMap) else CommentedMap()


def save_doc(path: Path, doc: CommentedMap) -> None:
    buf = StringIO()
    yaml_rt().dump(doc, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
    os.replace(tmp, path)


def dig(doc: CommentedMap, parts: list[str], create: bool) -> tuple[CommentedMap, str]:
    node = doc
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            if not create:
                raise KeyError(part)
            node[part] = CommentedMap()
        node = node[part]
    return node, parts[-1]


def has_path(doc: Any, parts: list[str]) -> bool:
    """True when the dotted path ``parts`` is set in ``doc`` (even to null)."""
    node = doc
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def settings_target(root: Path, file: Path, parts: list[str]) -> Path:
    """File a settings key is written to: ``settings.local.yaml`` when it already sets that key, else ``file``."""
    if file != SETTINGS_PATH or not parts:
        return file
    local = root / LOCAL_SETTINGS_PATH
    return LOCAL_SETTINGS_PATH if local.exists() and has_path(load_doc(local), parts) else file


# --------------------------------------------------------------------------- secrets (.env)


def _env_lines(root: Path) -> list[str]:
    env = root / ENV_FILE
    if env.exists():
        return env.read_text(encoding="utf-8").splitlines()
    example = root / ENV_EXAMPLE
    return example.read_text(encoding="utf-8").splitlines() if example.exists() else []


def secret_present(root: Path, name: str) -> bool:
    for line in _env_lines(root) if (root / ENV_FILE).exists() else []:
        k, _, v = line.partition("=")
        if k.strip() == name and v.split("#")[0].strip():
            return True
    return False


def set_secret(root: Path, name: str, value: str) -> None:
    """Create/update ``NAME=value`` in .env (created from .env.example). The value is never logged."""
    if "\n" in value or "\r" in value:
        raise ConfigError("Valeur de secret invalide (retour à la ligne)")
    lines, done = [], False
    for line in _env_lines(root):
        if line.split("=", 1)[0].strip() == name:
            lines.append(f"{name}={value.strip()}")
            done = True
        else:
            lines.append(line)
    if not done:
        lines.append(f"{name}={value.strip()}")
    (root / ENV_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
