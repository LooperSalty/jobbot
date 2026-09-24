"""AppContext: the bundle of config + services passed to every high-level function."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from jobbot.config import Profile, Secrets, Settings, load_profile, load_secrets, load_settings
from jobbot.db import Repository
from jobbot.llm import LLMClient, build_llm


@dataclass(frozen=True)
class AppContext:
    root: Path
    settings: Settings
    secrets: Secrets
    profile: Profile
    repo: Repository
    llm: LLMClient
    dry_run: bool = False
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("jobbot"))

    def path(self, relative: str) -> Path:
        """Resolve a settings path (relative to the project root) and make sure its parent exists."""
        p = Path(relative)
        p = p if p.is_absolute() else self.root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def output_dir(self) -> Path:
        d = self.path(self.settings.paths.output_dir + "/.keep").parent
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def build(cls, root: Path | None = None, *, dry_run: bool = False, llm: LLMClient | None = None) -> AppContext:
        root = (root or find_root()).resolve()
        settings = load_settings(root)
        secrets = load_secrets(root)
        profile = load_profile(root / settings.paths.profile)
        db_path = Path(settings.paths.db)
        repo = Repository(db_path if db_path.is_absolute() else root / db_path)
        return cls(
            root=root, settings=settings, secrets=secrets, profile=profile, repo=repo,
            llm=llm or build_llm(settings.llm, secrets), dry_run=dry_run,
        )


def find_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (or cwd) to the folder that holds config/ and pyproject.toml."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "config").is_dir():
            return candidate
    return Path(__file__).resolve().parents[2]
