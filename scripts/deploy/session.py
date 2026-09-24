"""The state shared by the deployment steps (configuration, runner, output, facts learned on the server)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import DeployConfig, DeployError
from .runner import Result, Runner


@dataclass
class Deployment:
    config: DeployConfig
    env_text: str  # the PC .env (never printed)
    root: Path  # local project root
    runner: Runner
    say: Callable[[str], None] = print
    dashboard_vpn: bool = False
    facts: dict[str, str] = field(default_factory=dict)  # learned by the check step (home, user…)
    notes: list[str] = field(default_factory=list)  # things to tell in the summary

    @property
    def dry_run(self) -> bool:
        return self.runner.dry_run

    def title(self, number: int | str, text: str) -> None:
        self.say(f"\n=== {number}. {text}")

    def require(self, result: Result, what: str) -> Result:
        """Raise a French DeployError when a (real) command failed."""
        if result.ok or self.dry_run:
            return result
        detail = (result.err or result.out).strip().splitlines()[-5:]
        raise DeployError(f"{what} : échec (code {result.code})" + (f"\n  {chr(10).join(detail)}" if detail else ""))

    def root_ssh(self, script: str, what: str, *, check: bool = True, show: str | None = None) -> Result:
        result = self.runner.ssh_run(self.config.root_alias, script, show=show)
        return self.require(result, what) if check else result

    def user_ssh(self, script: str, what: str, *, check: bool = True, data: bytes | None = None) -> Result:
        result = self.runner.ssh_run(self.config.project_alias, script, data=data)
        return self.require(result, what) if check else result
