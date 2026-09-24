"""Replacement of the previous application (« openclaw ») by jobbot — pure part: discovery parsing and the plan.

Discovery is dynamic (see ``replace_scripts``): systemd units (system + the project user's) whose name or
ExecStart / WorkingDirectory / EnvironmentFile mention a STOP pattern, docker containers (name / image / compose
project), pm2 processes, crontab lines (matched and masked on the server), processes listening on the old
application's port, files named after the patterns under the project user's home, /opt, /srv and /usr/local/bin.

- The project user's home path is ignored when matching (the account itself may be named after the old app).
- KEEP patterns always win: those services stay up even when discovery matches them (``jobbot`` is always kept).
- Trust: the root output is trusted; from the project account's output only units / unit lines / pm2 lines are
  read, forced to the « user » scope, and its paths must stay inside the home.
- Nothing here runs a command: ``replace_steps`` executes the plan (backup first, then stop).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

STOP_PATTERNS: tuple[str, ...] = ("openclaw", "crawl4ai")
KEEP_PATTERNS: tuple[str, ...] = ("searxng", "server-health-monitor", "syncthing", "jobbot")
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".path")
BACKUP_ROOTS = ("/opt/", "/srv/", "/usr/local/", "/etc/")
SYSTEM_UNIT_DIRS = ("/etc/systemd/system/", "/lib/systemd/system/", "/usr/lib/systemd/system/")
USER_TAGS = frozenset({"UNIT", "UNITLINE", "PM2"})  # the only lines read from the project account's output
_PATTERN_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]*$")
_SECRET_RE = re.compile(r"(?i)([A-Z0-9_-]*(?:BEARER|TOKEN|KEY|SECRET|PASS|PWD|AUTH)[A-Z0-9_-]*)(\s*[=:]\s*|\s+)(\S+)")
_LONG_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_URL_CRED_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


@dataclass(frozen=True)
class Unit:
    scope: str  # system | user
    name: str
    active: str = ""
    enabled: str = ""
    path: str = ""
    lines: tuple[str, ...] = ()  # ExecStart / WorkingDirectory / EnvironmentFile lines of the unit file

    def values(self, key: str) -> list[str]:
        return [line.split("=", 1)[1] for line in self.lines if line.split("=", 1)[0].strip() == key]


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str = ""
    state: str = ""
    project: str = ""
    workdir: str = ""
    restart: str = ""


@dataclass(frozen=True)
class Pm2App:
    owner: str  # root | user
    name: str
    cwd: str = ""
    exec_path: str = ""
    status: str = ""


@dataclass(frozen=True)
class Listener:
    port: int
    pid: int
    cwd: str = ""
    command: str = ""


@dataclass(frozen=True)
class Discovery:
    home: str = ""
    units: tuple[Unit, ...] = ()
    containers: tuple[Container, ...] = ()
    pm2: tuple[Pm2App, ...] = ()
    crontabs: dict[str, tuple[str, ...]] = field(default_factory=dict)  # owner → masked lines to remove
    listeners: tuple[Listener, ...] = ()
    paths: tuple[str, ...] = ()
    backups: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    stop_units: tuple[Unit, ...] = ()
    keep_units: tuple[Unit, ...] = ()
    stop_containers: tuple[Container, ...] = ()
    keep_containers: tuple[Container, ...] = ()
    stop_pm2: tuple[Pm2App, ...] = ()
    keep_pm2: tuple[Pm2App, ...] = ()
    cron_removed: dict[str, tuple[str, ...]] = field(default_factory=dict)  # masked previews (filtered on server)
    listeners: tuple[Listener, ...] = ()
    backup_paths: tuple[str, ...] = ()
    purge_paths: tuple[str, ...] = ()  # backup_paths minus anything holding a kept service

    @property
    def empty(self) -> bool:
        return not (self.stop_units or self.stop_containers or self.stop_pm2 or any(self.cron_removed.values())
                    or self.backup_paths)


# --------------------------------------------------------------------------- matching


def clean_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    """Lower-case patterns safe to embed in a shell regex (letters, digits, . _ @ -)."""
    return tuple(dict.fromkeys(p.strip().lower() for p in patterns if _PATTERN_RE.match(p.strip().lower())))


def normalized(text: str, home: str) -> str:
    """Lower-case text where the project user's home path is replaced by « ~ »."""
    lowered = (text or "").lower()
    return lowered.replace(home.lower().rstrip("/"), "~") if home and home.strip("/") else lowered


def matches(text: str, patterns: tuple[str, ...], home: str = "") -> bool:
    value = normalized(text, home)
    return any(p in value for p in patterns)


def redact(text: str) -> str:
    """Hide what looks like a secret before printing a crontab line or a command line."""
    text = _URL_CRED_RE.sub("://***@", text)
    text = _SECRET_RE.sub(lambda m: f"{m[1]}{m[2]}***", text)
    return _LONG_RE.sub("***", text)


def _under(path: str, home: str) -> bool:
    return bool(home) and path.startswith(home.rstrip("/") + "/")


# --------------------------------------------------------------------------- parsing


@dataclass
class _Acc:
    """Accumulator of ``parse_discovery`` (local to one parsing)."""

    home: str = ""
    units: dict[tuple[str, str], Unit] = field(default_factory=dict)
    lines: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    containers: list[Container] = field(default_factory=list)
    policies: dict[str, str] = field(default_factory=dict)
    pm2: list[Pm2App] = field(default_factory=list)
    crontabs: dict[str, list[str]] = field(default_factory=dict)
    listeners: list[Listener] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)


def _padded(parts: list[str], size: int) -> list[str]:
    return [*parts, *[""] * max(0, size - len(parts))]


def _on_unit(acc: _Acc, p: list[str]) -> None:
    p = _padded(p, 6)
    acc.units[(p[1], p[2])] = Unit(p[1], p[2], p[3], p[4], p[5])


def _on_docker(acc: _Acc, p: list[str]) -> None:
    p = _padded(p, 7)
    acc.containers.append(Container(id=p[1], name=p[2], image=p[3], state=p[4], project=p[5], workdir=p[6]))


def _on_pm2(acc: _Acc, p: list[str]) -> None:
    p = _padded(p, 6)
    acc.pm2.append(Pm2App(owner=p[1], name=p[2], cwd=p[3], exec_path=p[4], status=p[5]))


def _on_listen(acc: _Acc, p: list[str]) -> None:
    p = _padded(p, 5)
    if p[1].isdigit() and p[2].isdigit():
        acc.listeners.append(Listener(port=int(p[1]), pid=int(p[2]), cwd=p[3], command=p[4]))


def _on_path(acc: _Acc, p: list[str]) -> None:
    if p[1].startswith("/"):
        acc.paths.append(p[1].rstrip("/"))


_HANDLERS: dict[str, tuple[int, Callable[[_Acc, list[str]], None]]] = {  # tag → (minimum fields, handler)
    "HOME": (2, lambda acc, p: setattr(acc, "home", p[1].strip())),
    "UNIT": (3, _on_unit),
    "UNITLINE": (4, lambda acc, p: acc.lines.setdefault((p[1], p[2]), []).append("\t".join(p[3:]))),
    "DOCKER": (3, _on_docker),
    "DOCKERPOL": (3, lambda acc, p: acc.policies.__setitem__(p[1][:12], p[2])),
    "PM2": (3, _on_pm2),
    "CRONMATCH": (3, lambda acc, p: acc.crontabs.setdefault(p[1], []).append("\t".join(p[2:]))),
    "LISTEN": (3, _on_listen),
    "PATH": (2, _on_path),
    "BACKUP": (2, lambda acc, p: acc.backups.append(p[1].strip())),
}


def _user_lines(text: str) -> list[list[str]]:
    """Lines of the project account's output: units / unit lines / pm2 only, forced to the « user » scope."""
    rows = []
    for raw in text.splitlines():
        parts = raw.split("\t")
        if parts[0] in USER_TAGS and len(parts) >= 3:
            rows.append([parts[0], "user", *parts[2:]])
    return rows


def _safe_path(unit: Unit, home: str) -> Unit:
    """Unit file path kept only where such units live (system dirs / the user's systemd folder)."""
    ok = unit.path.startswith(SYSTEM_UNIT_DIRS) if unit.scope == "system" else \
        _under(unit.path, f"{home.rstrip('/')}/.config/systemd/user")
    return unit if ok else Unit(unit.scope, unit.name, unit.active, unit.enabled, "", unit.lines)


def parse_discovery(root_out: str, user_out: str = "") -> Discovery:
    """Tagged, tab-separated lines of the discovery scripts (root, project user) → ``Discovery``."""
    acc = _Acc()
    rows = [raw.split("\t") for raw in root_out.splitlines()] + _user_lines(user_out)
    for parts in rows:
        minimum, handler = _HANDLERS.get(parts[0], (0, None))
        if handler is not None and len(parts) >= minimum:
            handler(acc, parts)
    units = tuple(_safe_path(Unit(u.scope, u.name, u.active, u.enabled, u.path, tuple(acc.lines.get(key, []))),
                             acc.home) for key, u in sorted(acc.units.items()))
    return Discovery(
        home=acc.home, units=units,
        containers=tuple(Container(**{**c.__dict__, "restart": acc.policies.get(c.id[:12], "")})
                         for c in acc.containers),
        pm2=tuple(acc.pm2), crontabs={owner: tuple(lines) for owner, lines in acc.crontabs.items()},
        listeners=tuple(acc.listeners), paths=tuple(dict.fromkeys(acc.paths)),
        backups=tuple(sorted(set(acc.backups))),
    )


# --------------------------------------------------------------------------- plan


def _texts(unit: Unit) -> list[str]:
    keys = ("ExecStart", "ExecStartPre", "WorkingDirectory", "EnvironmentFile")
    return [unit.name, *(v for key in keys for v in unit.values(key))]


def _split_units(disc: Discovery, stop: tuple[str, ...], keep: tuple[str, ...]) -> tuple[tuple, tuple]:
    stopped, kept = [], []
    for unit in disc.units:
        texts = _texts(unit)
        if not unit.name.endswith(UNIT_SUFFIXES) or not any(matches(t, stop, disc.home) for t in texts):
            continue
        protected = matches(unit.name, keep) or any("jobbot" in normalized(t, disc.home) for t in texts)
        (kept if protected else stopped).append(unit)
    return tuple(stopped), tuple(kept)


def _split_containers(disc: Discovery, stop: tuple[str, ...], keep: tuple[str, ...]) -> tuple[tuple, tuple]:
    stopped, kept = [], []
    for c in disc.containers:
        labels = (c.name, c.image, c.project)
        if not (any(matches(t, stop) for t in labels) or matches(c.workdir, stop, disc.home)):
            continue
        (kept if any(matches(t, keep) for t in labels) else stopped).append(c)
    return tuple(stopped), tuple(kept)


def _split_pm2(disc: Discovery, stop: tuple[str, ...], keep: tuple[str, ...]) -> tuple[tuple, tuple]:
    stopped, kept = [], []
    for app in disc.pm2:
        if not any(matches(t, stop, disc.home) for t in (app.name, app.cwd, app.exec_path)):
            continue
        (kept if matches(app.name, keep) or "jobbot" in app.cwd.lower() else stopped).append(app)
    return tuple(stopped), tuple(kept)


def _exec_tokens(value: str) -> list[str]:
    return [t.strip("'\"") for t in value.lstrip("@-:+!").split() if t.strip("'\"").startswith("/")]


def _allowed(path: str, home: str, stop: tuple[str, ...], *, strict: bool) -> bool:
    """Under the home: named after the old app (any sub-path when not ``strict``: explicit extras); under
    /opt, /srv, /usr/local, /etc: only paths named after the old app; never system folders, the home itself,
    .ssh, backups or jobbot."""
    home = home.rstrip("/")
    if not path.startswith("/") or path in ("/", home) or "jobbot" in path.lower() or path.startswith("/etc/systemd"):
        return False
    if home and any(path == f"{home}/{d}" or path.startswith(f"{home}/{d}/") for d in (".ssh", "backups")):
        return False
    named = matches(path, stop, home)
    if _under(path, home):
        return named or not strict
    return path.startswith(BACKUP_ROOTS) and named


def _discovered(disc: Discovery, units: tuple[Unit, ...], containers: tuple[Container, ...],
                apps: tuple[Pm2App, ...]) -> list[str]:
    """Paths found automatically (always checked strictly); the project account's ones must be in the home."""
    found = [*disc.paths, *(c.workdir for c in containers)]
    for unit in units:
        paths = [v.strip().lstrip("-").strip("'\"") for key in ("WorkingDirectory", "EnvironmentFile")
                 for v in unit.values(key)]
        paths += [t for key in ("ExecStart", "ExecStartPre") for v in unit.values(key) for t in _exec_tokens(v)]
        found += [p for p in paths if unit.scope == "system" or _under(p, disc.home)]
    for app in apps:
        found += [p for p in (app.cwd, app.exec_path) if app.owner == "root" or _under(p, disc.home)]
    return found


def backup_paths(disc: Discovery, units: tuple[Unit, ...], containers: tuple[Container, ...],
                 apps: tuple[Pm2App, ...], stop: tuple[str, ...], extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Folders / files of the old application to archive (``extra``: explicit, relative to the home)."""
    home = disc.home.rstrip("/")
    chosen = [p.rstrip("/") for p in _discovered(disc, units, containers, apps)
              if p and _allowed(p.rstrip("/"), home, stop, strict=True)]
    chosen += [p for p in (f"{home}/{rel.strip('/')}" for rel in extra if home) if _allowed(p, home, stop, strict=False)]
    unique = sorted(dict.fromkeys(chosen))
    return tuple(p for p in unique if not any(p != q and p.startswith(q + "/") for q in unique))


def _kept_locations(units: tuple[Unit, ...], containers: tuple[Container, ...], apps: tuple[Pm2App, ...]) -> list[str]:
    places = [c.workdir for c in containers] + [a.cwd for a in apps]
    places += [v.strip().lstrip("-") for u in units for v in u.values("WorkingDirectory")]
    return [p.rstrip("/") for p in places if p.startswith("/")]


def purge_paths(paths: tuple[str, ...], kept: list[str]) -> tuple[str, ...]:
    """Never delete a folder that is (or contains) the folder of a service that stays up."""
    return tuple(p for p in paths if not any(k == p or k.startswith(p + "/") for k in kept))


def build_plan(disc: Discovery, *, stop: tuple[str, ...] = STOP_PATTERNS, keep: tuple[str, ...] = KEEP_PATTERNS,
               extra: tuple[str, ...] = ()) -> Plan:
    stop, keep = clean_patterns(stop), clean_patterns((*KEEP_PATTERNS, *keep))
    stop_units, keep_units = _split_units(disc, stop, keep)
    stop_containers, keep_containers = _split_containers(disc, stop, keep)
    stop_pm2, keep_pm2 = _split_pm2(disc, stop, keep)
    saved = backup_paths(disc, stop_units, stop_containers, stop_pm2, stop, extra)
    return Plan(stop_units=stop_units, keep_units=keep_units, stop_containers=stop_containers,
                keep_containers=keep_containers, stop_pm2=stop_pm2, keep_pm2=keep_pm2,
                cron_removed=dict(disc.crontabs), listeners=disc.listeners, backup_paths=saved,
                purge_paths=purge_paths(saved, _kept_locations(keep_units, keep_containers, keep_pm2)))


# --------------------------------------------------------------------------- description (French) & manifest


def _short(path: str, home: str) -> str:
    return "~" + path[len(home.rstrip("/")):] if _under(path, home) else path


def _unit_line(unit: Unit) -> str:
    kind = "minuterie" if unit.name.endswith(".timer") else "service"
    scope = "système" if unit.scope == "system" else "utilisateur"
    return f"{kind} {scope} {unit.name} ({unit.active or '?'}, {unit.enabled or '?'})"


def describe(plan: Plan, disc: Discovery, port: int) -> list[str]:
    home = disc.home
    stop = [_unit_line(u) for u in plan.stop_units]
    stop += [f"conteneur docker {c.name} ({c.state or '?'}, image {c.image or '?'})" for c in plan.stop_containers]
    stop += [f"processus pm2 ({a.owner}) « {a.name} » ({a.status or '?'})" for a in plan.stop_pm2]
    stop += [f"ligne crontab ({owner}) retirée : {redact(line)}" for owner, lines in plan.cron_removed.items()
             for line in lines]
    keep = [_unit_line(u) for u in plan.keep_units] + [f"conteneur docker {c.name}" for c in plan.keep_containers]
    keep += [f"processus pm2 « {a.name} »" for a in plan.keep_pm2]
    lines = ["À ARRÊTER (après sauvegarde ; rien n'est supprimé) :", *(f"  • {x}" for x in stop or ["rien"])]
    lines += ["CONSERVÉS (laissés en service) :", *(f"  • {x}" for x in keep or ["rien de concerné"])]
    lines += [f"Port {port} : " + ("; ".join(f"pid {lst.pid} {redact(lst.command)[:80]}" for lst in plan.listeners)
                                   if plan.listeners else "libre")]
    lines += ["Sauvegardé dans ~/backups/openclaw-<date>.tar.gz (root, droits 600) :",
              *(f"  • {_short(p, home)}" for p in plan.backup_paths or ["(aucun dossier)"]),
              "  • + fichiers des services, crontabs, docker inspect, état pm2"]
    return lines


def manifest_json(plan: Plan, disc: Discovery, *, user: str, created: str, port: int,
                  stop: tuple[str, ...] = STOP_PATTERNS, keep: tuple[str, ...] = KEEP_PATTERNS) -> str:
    """What the backup contains and how to bring it back (read by the restore helper). No crontab content:
    the removed lines are recomputed on the server from the saved crontabs and these patterns."""
    data = {
        "created": created, "user": user, "home": disc.home, "port": port,
        "stop_patterns": list(clean_patterns(stop)), "keep_patterns": list(clean_patterns((*KEEP_PATTERNS, *keep))),
        "units": [{"scope": u.scope, "name": u.name, "path": u.path, "active": u.active, "enabled": u.enabled}
                  for u in plan.stop_units],
        "containers": [{"id": c.id, "name": c.name, "restart": c.restart or "no"} for c in plan.stop_containers],
        "pm2": [{"owner": a.owner, "name": a.name} for a in plan.stop_pm2],
        "paths": list(plan.backup_paths),
    }
    return json.dumps(data, ensure_ascii=False, indent=1)
