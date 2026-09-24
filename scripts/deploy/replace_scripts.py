"""Remote scripts of the replacement step. Placeholders ``@@NAME@@`` are filled with shell-quoted values.

Security rules (root scripts working next to a possibly compromised project account):
- anything inside the project user's home is read / written / deleted AS that user (``runuser``) or after a
  symlink check, never blindly as root;
- crontab lines are filtered ON the server (``CRON_AWK``): no crontab content travels to the PC, only masked
  previews of the lines that will be removed;
- pm2 is only queried when its daemon already runs (``jlist`` would start it), and only name / folder / script /
  status leave the server (never the environment).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

from .replace_plan import KEEP_PATTERNS, STOP_PATTERNS

BACKUP_DIR = "backups"
ARCHIVE_PREFIX = "openclaw-"
ARCHIVE_RE = re.compile(r"^openclaw-\d{8}-\d{6}\.tar\.gz$")
UNIT_KEYS = "ExecStart|ExecStartPre|WorkingDirectory|EnvironmentFile"
UNIT_DIRS = "/etc/systemd/system /lib/systemd/system /usr/lib/systemd/system"
_ERE_SPECIAL = re.compile(r"([.\[\]()*+?{}|^$\\])")


def _fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"@@{key}@@", value)
    return template


def _words(items: Iterable[str]) -> str:
    return " ".join(shlex.quote(i) for i in items)


def regex(patterns: tuple[str, ...]) -> str:
    """POSIX ERE alternation of literal, lower-case patterns (grep -E, awk)."""
    return "|".join(_ERE_SPECIAL.sub(r"\\\1", p) for p in patterns)


# awk program (mawk / gawk): the project user's home is replaced by « ~ » before matching, KEEP patterns and
# jobbot's own lines always stay. MODE: keep (print kept lines) | match (lines to remove) | show (masked preview).
CRON_AWK = r"""
function norm(s,    l, h, n) {
  l = tolower(s); h = tolower(H)
  if (h != "") { n = index(l, h); while (n > 0) { l = substr(l, 1, n - 1) "~" substr(l, n + length(h)); n = index(l, h) } }
  return l
}
function mask(s,    i, n, f, out, eq) {
  n = split(s, f, " "); out = ""
  for (i = 1; i <= n; i++) {
    eq = index(f[i], "=")
    if (i > 1 && tolower(f[i - 1]) ~ /bearer/) f[i] = "***"
    else if (eq > 0) f[i] = substr(f[i], 1, eq) "***"
    else if (length(f[i]) >= 24 && index(f[i], "/") == 0) f[i] = "***"
    out = out (i > 1 ? " " : "") f[i]
  }
  return out
}
{
  l = norm($0)
  hit = (l ~ RX) && !(KX != "" && l ~ KX) && index($0, "# jobbot:") == 0
  if (MODE == "keep" && !hit) print
  else if (MODE == "match" && hit) print
  else if (MODE == "show" && hit) print mask($0)
}
"""

CRON_FUNCTION = """
cron_filter() { awk -v H="$H" -v RX="$RX" -v KX="$KX" -v MODE="$1" '@@AWK@@'; }
""".replace("@@AWK@@", CRON_AWK.strip())

PM2_FUNCTION = r"""
pm2_list() {
  command -v pm2 >/dev/null 2>&1 || return 0
  [ -S "${PM2_HOME:-$HOME/.pm2}/rpc.sock" ] || return 0
  pm2 jlist 2>/dev/null | python3 -c 'import json, sys
owner = sys.argv[1]
apps = []
for line in reversed(sys.stdin.read().splitlines()):
    if line.startswith("["):
        try:
            apps = json.loads(line)
            break
        except ValueError:
            continue
for a in apps:
    e = a.get("pm2_env") or {}
    print("\t".join(["PM2", owner, str(a.get("name", "")), str(e.get("pm_cwd", "")), str(e.get("pm_exec_path", "")), str(e.get("status", ""))]))' "$1"
}
"""

UNITS_BLOCK = r"""
{ { @@CTL@@ list-unit-files --no-legend --plain 2>/dev/null | awk '{print $1}'
    @@CTL@@ list-units --all --no-legend --plain 2>/dev/null | awk '{ if ($1 == "●" || $1 == "*") print $2; else print $1 }'
  } | grep -iE "$RX"
  grep -rliE "^(@@KEYS@@)=.*($RX)" @@DIRS@@ 2>/dev/null | xargs -r -n1 basename
} | grep -E '\.(service|timer|socket|path)$' | sort -u | while IFS= read -r u; do
  frag=$(@@CTL@@ show -p FragmentPath --value "$u" 2>/dev/null)
  printf 'UNIT\t@@SCOPE@@\t%s\t%s\t%s\t%s\n' "$u" "$(@@CTL@@ is-active "$u" 2>/dev/null)" "$(@@CTL@@ is-enabled "$u" 2>/dev/null)" "$frag"
  [ -f "$frag" ] && grep -E '^(@@KEYS@@)=' "$frag" | while IFS= read -r l; do printf 'UNITLINE\t@@SCOPE@@\t%s\t%s\n' "$u" "$l"; done
done
"""

LISTEN_BLOCK = r"""
ss -ltnpH "sport = :@@PORT@@" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | while read -r pid; do
  printf 'LISTEN\t%s\t%s\t%s\t%s\n' @@PORT@@ "$pid" "$(readlink /proc/$pid/cwd 2>/dev/null | tr '\n\t' '  ')" "$(tr '\0\n\t' '   ' < /proc/$pid/cmdline 2>/dev/null | cut -c1-300)"
done
"""

HEADER = r"""U=@@USER@@
H=$(getent passwd "$U" | cut -d: -f6)
RX=@@RX@@
KX=@@KX@@
"""


def _header(user: str, stop: tuple[str, ...], keep: tuple[str, ...]) -> str:
    return _fill(HEADER, USER=shlex.quote(user), RX=shlex.quote(regex(stop)), KX=shlex.quote(regex(keep)))


def listeners_script(port: int) -> str:
    return _fill(LISTEN_BLOCK, PORT=str(int(port)))


# --------------------------------------------------------------------------- discovery (read-only)


def discovery_root_script(user: str, stop: tuple[str, ...], keep: tuple[str, ...], port: int) -> str:
    names = " -o ".join(f"-iname {shlex.quote('*' + p + '*')}" for p in stop)
    body = _header(user, stop, keep) + r"""printf 'HOME\t%s\n' "$H"
""" + _fill(UNITS_BLOCK, CTL="systemctl", SCOPE="system", DIRS=UNIT_DIRS) + PM2_FUNCTION + CRON_FUNCTION + r"""
pm2_list root
if command -v docker >/dev/null 2>&1; then
  docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sed 's/^/DOCKER\t/'
  ids=$(docker ps -aq 2>/dev/null)
  [ -n "$ids" ] && docker inspect -f '{{.Id}} {{.HostConfig.RestartPolicy.Name}}' $ids 2>/dev/null | awk '{printf "DOCKERPOL\t%s\t%s\n", substr($1,1,12), $2}'
fi
crontab -l 2>/dev/null | cron_filter show | sed 's/^/CRONMATCH\troot\t/'
crontab -u "$U" -l 2>/dev/null | cron_filter show | sed 's/^/CRONMATCH\tuser\t/'
""" + LISTEN_BLOCK + r"""
[ -n "$H" ] && find "$H" -mindepth 1 -maxdepth 1 \( @@NAMES@@ \) ! -name '*[[:cntrl:]]*' 2>/dev/null | sed 's/^/PATH\t/'
find /usr/local/bin /opt /srv -mindepth 1 -maxdepth 1 \( @@NAMES@@ \) ! -name '*[[:cntrl:]]*' 2>/dev/null | sed 's/^/PATH\t/'
ls -1 "$H/@@BACKUPS@@"/@@PREFIX@@*.tar.gz 2>/dev/null | sed 's/^/BACKUP\t/'
exit 0
"""
    return _fill(body, KEYS=UNIT_KEYS, NAMES=names, PORT=str(int(port)), BACKUPS=BACKUP_DIR, PREFIX=ARCHIVE_PREFIX)


def discovery_user_script(stop: tuple[str, ...]) -> str:
    body = ('export XDG_RUNTIME_DIR="/run/user/$(id -u)"\nRX=@@RX@@\n'
            + _fill(UNITS_BLOCK, CTL="systemctl --user", SCOPE="user", DIRS='"$HOME/.config/systemd/user"')
            + PM2_FUNCTION + "pm2_list user\nexit 0\n")
    return _fill(body, RX=shlex.quote(regex(stop)), KEYS=UNIT_KEYS)


# --------------------------------------------------------------------------- backup / stop


BACKUP_BODY = r"""set -eu
umask 077
[ -n "$H" ] && [ -d "$H" ] || { echo "HOME_MISSING"; exit 1; }
DEST="$H/@@BACKUPS@@"
[ -L "$DEST" ] && { echo "BACKUP_DIR_IS_A_LINK"; exit 1; }
install -d -m 700 -o root -g root "$DEST"
STAGE=$(mktemp -d)
PART=""
trap 'rm -rf "$STAGE"; [ -z "$PART" ] || rm -f "$PART"' EXIT
mkdir -p "$STAGE/meta/units/system" "$STAGE/meta/units/user"
copy_unit() {
  if [ "$1" = system ]; then
    if [ -f "$2" ]; then cp "$2" "$STAGE/meta/units/system/"; fi
  else
    runuser -u "$U" -- cat "$2" > "$STAGE/meta/units/user/$(basename "$2")" 2>/dev/null || true
  fi
}
@@UNITS@@
crontab -l > "$STAGE/meta/crontab-root.txt" 2>/dev/null || true
crontab -u "$U" -l > "$STAGE/meta/crontab-user.txt" 2>/dev/null || true
if [ -f /root/.pm2/dump.pm2 ]; then cp /root/.pm2/dump.pm2 "$STAGE/meta/pm2-root.dump"; fi
runuser -u "$U" -- cat "$H/.pm2/dump.pm2" > "$STAGE/meta/pm2-user.dump" 2>/dev/null || rm -f "$STAGE/meta/pm2-user.dump"
if [ -n "@@CONTAINERS@@" ] && command -v docker >/dev/null 2>&1; then
  docker inspect @@CONTAINERS@@ > "$STAGE/meta/docker-inspect.json" 2>/dev/null || true
fi
cat > "$STAGE/meta/MANIFEST.json" <<'JOBBOT_MANIFEST'
@@MANIFEST@@
JOBBOT_MANIFEST
set --
for p in @@PATHS@@; do
  if [ -n "$p" ] && { [ -e "$p" ] || [ -L "$p" ]; }; then set -- "$@" "${p#/}"; fi
done
PART=$(mktemp "$DEST/.@@PREFIX@@XXXXXX")
rc=0
tar -czf "$PART" -C "$STAGE" meta -C / "$@" || rc=$?
[ "$rc" -le 1 ] || { echo "TAR_FAILED=$rc"; exit 1; }
ARCHIVE="$DEST/@@PREFIX@@@@STAMP@@.tar.gz"
mv -f "$PART" "$ARCHIVE"
PART=""
chmod 600 "$ARCHIVE"
echo "BACKUP=$ARCHIVE"
echo "SIZE=$(du -h "$ARCHIVE" | cut -f1)"
"""


def backup_script(user: str, stamp: str, manifest: str, units: list[tuple[str, str]], paths: list[str],
                  containers: list[str]) -> str:
    """Root: archive (root-owned, 600) of the paths + unit files + crontabs + docker inspect + pm2 dumps. GNU tar
    code 1 (« file changed as we read it »: the app still runs) is accepted."""
    copies = "\n".join(f"copy_unit {'system' if scope == 'system' else 'user'} {shlex.quote(path)}"
                       for scope, path in units if path)
    body = _header(user, (), ()) + BACKUP_BODY
    return _fill(body, BACKUPS=BACKUP_DIR, PREFIX=ARCHIVE_PREFIX, STAMP=re.sub(r"[^0-9-]", "", stamp),
                 UNITS=copies or ":", CONTAINERS=_words(containers), MANIFEST=manifest, PATHS=_words(paths) or '""')


def stop_script(units: list[str], containers: list[str], pm2: list[str], *, user_scope: bool) -> str:
    """Stop + disable units (timers first), containers (no restart) and pm2 processes. Reversible."""
    ctl = "systemctl --user" if user_scope else "systemctl"
    ordered = sorted(units, key=lambda u: (not u.endswith(".timer"), u))
    lines = ['export XDG_RUNTIME_DIR="/run/user/$(id -u)"'] if user_scope else []
    lines += [f'{ctl} stop {shlex.quote(u)} 2>/dev/null; {ctl} disable {shlex.quote(u)} >/dev/null 2>&1; '
              f"printf 'UNIT\\t%s\\t%s\\n' {shlex.quote(u)} \"$({ctl} is-active {shlex.quote(u)} 2>/dev/null)\""
              for u in ordered]
    lines += [f"docker update --restart=no {shlex.quote(c)} >/dev/null 2>&1; docker stop {shlex.quote(c)} "
              f">/dev/null 2>&1; printf 'DOCKER\\t%s\\tstopped\\n' {shlex.quote(c)}" for c in containers]
    lines += [f"pm2 delete {shlex.quote(n)} >/dev/null 2>&1 && printf 'PM2\\t%s\\tdeleted\\n' {shlex.quote(n)}"
              for n in pm2]
    if pm2:
        lines.append("pm2 save --force >/dev/null 2>&1 || true")
    return "\n".join([*lines, "exit 0"]) + "\n"


CRON_CLEAN_BODY = r"""set -u
umask 077
clean_cron() {
  owner=$1; shift
  cur=$(mktemp); new=$(mktemp)
  if crontab "$@" -l > "$cur" 2>/dev/null; then
    n=$(cron_filter match < "$cur" | wc -l)
    if [ "$n" -gt 0 ]; then
      cron_filter keep < "$cur" > "$new"
      crontab "$@" "$new" && printf 'CRONREMOVED\t%s\t%s\n' "$owner" "$n"
    fi
  fi
  rm -f "$cur" "$new"
}
clean_cron root
clean_cron user -u "$U"
exit 0
"""


def cron_clean_script(user: str, stop: tuple[str, ...], keep: tuple[str, ...]) -> str:
    """Root: remove the old application's crontab lines (root + project user), on the server only."""
    return _header(user, stop, keep) + CRON_FUNCTION + CRON_CLEAN_BODY


def kill_script(pids: list[int]) -> str:
    words = " ".join(str(int(p)) for p in pids)
    return (f"for pid in {words}; do kill \"$pid\" 2>/dev/null; done; sleep 2\n"
            f"for pid in {words}; do kill -0 \"$pid\" 2>/dev/null && kill -9 \"$pid\"; done; exit 0\n")


# --------------------------------------------------------------------------- purge


PURGE_BODY = r"""set -u
[ -n "$H" ] && [ -d "$H" ] || { echo "HOME_MISSING"; exit 1; }
H=$(realpath -e "$H")
purge_path() {
  p=$1
  case "$p" in ""|/|"$H"|"$H/"|"$H/.ssh"|"$H/.ssh/"*|"$H/@@BACKUPS@@"|"$H/@@BACKUPS@@/"*|*jobbot*) printf 'SKIPPED\t%s\n' "$p"; return 0 ;; esac
  case "$H/" in "$p"/*) printf 'SKIPPED\t%s\n' "$p"; return 0 ;; esac
  if [ -L "$p" ]; then rm -f -- "$p" && printf 'REMOVED\t%s\n' "$p"; return 0; fi
  [ -e "$p" ] || return 0
  real=$(realpath -e -- "$p") || return 0
  if [ "$real" != "$p" ]; then printf 'SKIPPED\t%s\n' "$p"; return 0; fi
  rm -rf -- "$p" && printf 'REMOVED\t%s\n' "$p"
}
for f in @@SYSTEMUNITS@@; do [ -n "$f" ] && rm -f -- "$f" && printf 'REMOVED\t%s\n' "$f"; done
for f in @@USERUNITS@@; do [ -n "$f" ] && runuser -u "$U" -- rm -f -- "$f" && printf 'REMOVED\t%s\n' "$f"; done
systemctl daemon-reload
for c in @@CONTAINERS@@; do [ -n "$c" ] && docker rm -f "$c" >/dev/null 2>&1 && printf 'REMOVED\tdocker %s\n' "$c"; done
for p in @@PATHS@@; do purge_path "$p"; done
exit 0
"""


def purge_script(user: str, system_units: list[str], user_units: list[str], containers: list[str],
                 paths: list[str]) -> str:
    """Root: delete unit files, containers and folders of the old application (never the home, .ssh, backups,
    jobbot, a parent of the home, or a path reached through a symbolic link)."""
    body = _header(user, (), ()) + PURGE_BODY
    return _fill(body, BACKUPS=BACKUP_DIR, SYSTEMUNITS=_words(system_units) or '""',
                 USERUNITS=_words(user_units) or '""', CONTAINERS=_words(containers) or '""',
                 PATHS=_words(paths) or '""')


# --------------------------------------------------------------------------- restore


def latest_archive_script(user: str) -> str:
    return (f'H=$(getent passwd {shlex.quote(user)} | cut -d: -f6)\n'
            f'ls -1t "$H/{BACKUP_DIR}"/{ARCHIVE_PREFIX}*.tar.gz 2>/dev/null | head -1\nexit 0\n')


def manifest_script(user: str, archive_name: str) -> str:
    return (f'H=$(getent passwd {shlex.quote(user)} | cut -d: -f6)\n'
            f'tar -xzOf "$H/{BACKUP_DIR}/{archive_name}" meta/MANIFEST.json\n')


RESTORE_BODY = r"""set -u
umask 077
A="$H/@@BACKUPS@@/@@ARCHIVE@@"
if [ ! -f "$A" ] || [ -L "$A" ]; then echo "ARCHIVE_MISSING"; exit 1; fi
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$A" -C "$STAGE" meta
for p in @@PATHS@@; do
  if [ -n "$p" ] && [ ! -e "/$p" ] && [ ! -L "/$p" ]; then tar -xzf "$A" -C / "$p" && printf 'RESTORED\t/%s\n' "$p"; fi
done
restore_system_unit() {
  if [ ! -e "$2" ] && [ -f "$STAGE/meta/units/system/$1" ]; then cp "$STAGE/meta/units/system/$1" "$2" && printf 'RESTORED\t%s\n' "$2"; fi
}
restore_user_unit() {
  runuser -u "$U" -- test -e "$2" && return 0
  [ -s "$STAGE/meta/units/user/$1" ] || return 0
  runuser -u "$U" -- sh -c 'mkdir -p "$(dirname "$1")" && cat > "$1"' sh "$2" < "$STAGE/meta/units/user/$1" && printf 'RESTORED\t%s\n' "$2"
}
@@UNITFILES@@
systemctl daemon-reload
for u in @@ENABLE@@; do [ -n "$u" ] && systemctl enable "$u" >/dev/null 2>&1; done
for u in @@START@@; do if [ -n "$u" ]; then systemctl start "$u"; printf 'UNIT\t%s\t%s\n' "$u" "$(systemctl is-active "$u")"; fi; done
@@DOCKER@@
if [ -f "$STAGE/meta/pm2-root.dump" ] && command -v pm2 >/dev/null 2>&1; then
  mkdir -p /root/.pm2 && cp "$STAGE/meta/pm2-root.dump" /root/.pm2/dump.pm2 && pm2 resurrect >/dev/null 2>&1 && printf 'RESTORED\tpm2 root\n'
fi
if [ -s "$STAGE/meta/pm2-user.dump" ]; then
  runuser -u "$U" -- sh -c 'mkdir -p "$1" && cat > "$1/dump.pm2"' sh "$H/.pm2" < "$STAGE/meta/pm2-user.dump"
fi
restore_cron() {
  owner=$1; shift
  f="$STAGE/meta/crontab-$owner.txt"
  [ -s "$f" ] || return 0
  cur=$(mktemp); add=$(mktemp)
  crontab "$@" -l > "$cur" 2>/dev/null || : > "$cur"
  cron_filter match < "$f" | while IFS= read -r line; do grep -Fxq -- "$line" "$cur" || printf '%s\n' "$line" >> "$add"; done
  if [ -s "$add" ]; then cat "$add" >> "$cur"; crontab "$@" "$cur" && printf 'RESTORED\tcrontab %s\n' "$owner"; fi
  rm -f "$cur" "$add"
}
restore_cron root
restore_cron user -u "$U"
exit 0
"""


def _restore_units(units: list[dict], home: str) -> str:
    lines = []
    for unit in units:
        path, name = str(unit.get("path") or ""), shlex.quote(str(unit.get("path") or "").rsplit("/", 1)[-1])
        if unit.get("scope") == "system" and path.startswith("/etc/systemd/system/"):
            lines.append(f"restore_system_unit {name} {shlex.quote(path)}")
        elif unit.get("scope") == "user" and home and path.startswith(home.rstrip("/") + "/.config/systemd/user/"):
            lines.append(f"restore_user_unit {name} {shlex.quote(path)}")
    return "\n".join(lines) or ":"


def restore_root_script(user: str, archive_name: str, manifest: dict) -> str:
    """Root: missing files back from the archive, unit files, system units, containers, pm2 dumps, crontab lines
    (recomputed on the server from the saved crontabs with the patterns of the manifest)."""
    units = manifest.get("units", [])
    system = [u for u in units if u.get("scope") == "system"]
    docker = [f"docker update --restart={shlex.quote(c.get('restart') or 'no')} {shlex.quote(c['id'])} >/dev/null "
              f"2>&1; docker start {shlex.quote(c['id'])} >/dev/null 2>&1 && printf 'RESTORED\\tdocker %s\\n' "
              f"{shlex.quote(c['name'])}" for c in manifest.get("containers", [])]
    stop = tuple(manifest.get("stop_patterns") or STOP_PATTERNS)  # never empty: an empty regex matches all
    keep = tuple(manifest.get("keep_patterns") or KEEP_PATTERNS)
    body = _header(user, stop, keep) + CRON_FUNCTION + RESTORE_BODY
    return _fill(body, BACKUPS=BACKUP_DIR, ARCHIVE=archive_name,
                 PATHS=_words(p.lstrip("/") for p in manifest.get("paths", [])) or '""',
                 UNITFILES=_restore_units(units, str(manifest.get("home") or "")),
                 ENABLE=_words(u["name"] for u in system if u.get("enabled") == "enabled") or '""',
                 START=_words(u["name"] for u in system if u.get("active") == "active") or '""',
                 DOCKER="\n".join(docker) or ":")


def restore_user_script(manifest: dict) -> str:
    units = [u for u in manifest.get("units", []) if u.get("scope") == "user"]
    lines = ['export XDG_RUNTIME_DIR="/run/user/$(id -u)"', "systemctl --user daemon-reload"]
    lines += [f"systemctl --user enable {shlex.quote(u['name'])} >/dev/null 2>&1" for u in units
              if u.get("enabled") == "enabled"]
    lines += [f"systemctl --user start {shlex.quote(u['name'])}" for u in units if u.get("active") == "active"]
    if any(a.get("owner") == "user" for a in manifest.get("pm2", [])):
        lines.append("command -v pm2 >/dev/null 2>&1 && pm2 resurrect >/dev/null 2>&1 && printf 'RESTORED\\tpm2 user\\n'")
    return "\n".join([*lines, "exit 0"]) + "\n"
