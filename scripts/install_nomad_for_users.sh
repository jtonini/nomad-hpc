#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
#
# Install nomad once for everyone on a cluster, in /usr/local/sw/nomad (or
# NOMAD_USERS_DIR, a full path), so users can run `nomad size` and
# `nomad submit`. Run it as the account that owns that folder, on the
# cluster or from a nomad checkout:
#
#     bash install_nomad_for_users.sh 1.18.1
#     ssh OWNER@CLUSTER 'bash -s 1.18.1' < scripts/install_nomad_for_users.sh
#     ssh OWNER@CLUSTER 'NOMAD_USERS_DIR=/path bash -s 1.18.1' < scripts/install_nomad_for_users.sh
#
# Running it again with another version upgrades: that is how the cluster
# follows nomad's releases (docs/submit.md).
#
# - Each release is whole in its own folder, releases/VERSION: its own
#   Python 3.12 (installed by uv, which comes from its GitHub release,
#   checked against the checksum below) and its own environment. It is
#   built and checked first; then bin/nomad is replaced, in one step, by
#   one that runs it. Until then users go on with the one they had, and a
#   release that fails is removed. The one before is kept: to go back, see
#   the end.
# - bin/nomad starts that Python isolated (-I), so PYTHONHOME and
#   PYTHONPATH set for another Python don't reach it; the jobs it submits
#   get the environment as it is. check_current.sh says when a newer
#   release is out.
# - Nothing is left writable by anyone but the owner, nor owned by anyone
#   else; uv's cache and downloads are in a private temporary folder; one
#   run at a time.
set -euo pipefail
V=${1:?give the version, e.g. 1.18.1}
UVV=0.13.5
UV_SHA256_x86_64=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f
UV_SHA256_aarch64=4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403
DEFAULT_D=/usr/local/sw/nomad
D=${NOMAD_USERS_DIR:-$DEFAULT_D}
umask 022
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV CONDA_PREFIX
export UV_NO_CONFIG=1 UV_LINK_MODE=copy UV_MANAGED_PYTHON=1
me=$(id -un)

stop() { echo "$*: stopping" >&2; exit 1; }
plain_path() {
    case "$1" in
        /*) ;;
        *) stop "$1: give the folder as a full path" ;;
    esac
    case "$1" in
        *[!A-Za-z0-9/._-]*) stop "$1: give a folder whose path has only letters, digits and / . _ -" ;;
    esac
}

# A release of nomad, 1.18.0 or later (no pre-releases).
if ! printf '%s\n' "$V" | grep -Eq '^[0-9]+(\.[0-9]+)*(\.post[0-9]+)?$'; then
    stop "not a release's version: $V"
fi
if [ "$(printf '%s\n' 1.18.0 "$V" | sort -V | head -1)" != 1.18.0 ]; then
    stop "nomad-hpc $V has no nomad size and nomad submit (1.18.0 or later)"
fi

# The folder: this account's, and nomad's (or empty, or new). Everything in
# it must be what this script or 1.18.0's makes, or nothing is touched.
plain_path "$D"
if [ -L "$D" ]; then stop "$D is a link: give the folder itself"; fi
if [ -e "$D" ] && [ ! -d "$D" ]; then stop "$D exists and isn't a folder"; fi
given=$D
if [ -d "$D" ]; then
    owner=$(stat -c %U "$D")
    if [ "$owner" != "$me" ]; then stop "$D belongs to $owner: run this as $owner"; fi
else
    mkdir -p "$D"
fi
D=$(cd "$D" && pwd -P)
plain_path "$D"
not_ours() { stop "$D isn't a nomad install ($1): give another folder"; }
only() {
    # $1 a folder; then the names (patterns) that may be in it
    local dir=$1 x n p ok
    shift
    for x in "$dir"/* "$dir"/.[!.]*; do
        if [ ! -e "$x" ] && [ ! -L "$x" ]; then continue; fi
        n=$(basename "$x")
        ok=""
        for p in "$@" '.nfs*'; do
            case "$n" in $p) ok=yes; break ;; esac
        done
        if [ -z "$ok" ]; then not_ours "${x#"$D"/} is there"; fi
    done
}
old18() {
    # 1.18.0's venv/: on the Python it put in python/
    grep -Eq "^home = ($D|$given)/python/" "$D/venv/pyvenv.cfg" 2>/dev/null
}
only "$D" bin releases uv current check_current.sh .lock .nomad-removed venv python tools current.new check_current.sh.new
for x in bin releases uv venv python tools .nomad-removed; do
    if [ -L "$D/$x" ] || { [ -e "$D/$x" ] && [ ! -d "$D/$x" ]; }; then not_ours "$x isn't a folder"; fi
done
if [ -d "$D/bin" ]; then only "$D/bin" nomad nomad.before nomad.new nomad.before.new; fi
for x in nomad nomad.before; do
    if [ -e "$D/bin/$x" ] && ! grep -q "nomad.cli import main" "$D/bin/$x"; then not_ours "bin/$x isn't nomad's"; fi
done
if [ -d "$D/uv" ]; then only "$D/uv" 'uv-*'; fi
if [ -d "$D/releases" ]; then
    only "$D/releases" '[0-9]*'
    for r in "$D"/releases/*; do
        if [ -d "$r" ] && [ ! -L "$r" ]; then only "$r" python venv; else [ ! -e "$r" ] || not_ours "${r#"$D"/} isn't a folder"; fi
    done
fi
if [ -e "$D/current" ] && [ ! -L "$D/current" ]; then not_ours "current isn't a link"; fi
if [ -e "$D/check_current.sh" ] && ! grep -q "nomad for users" "$D/check_current.sh"; then
    not_ours "check_current.sh isn't nomad's"
fi
if [ -d "$D/venv" ] && ! old18; then not_ours "venv/ isn't 1.18.0's"; fi
if [ -d "$D/python" ]; then
    only "$D/python" 'cpython-*' .lock .temp .cache .gitignore
    if [ ! -d "$D/tools" ]; then not_ours "python/ without 1.18.0's tools/"; fi
fi
if [ -d "$D/tools" ] && [ -n "$(find "$D/tools" -not -type d ! -path "$D/tools/bin/uv" ! -path "$D/tools/bin/uvx" \
        ! -path "$D/tools/uv/*" ! -path "$D/tools/uv-*.dist-info/*" -print -quit)" ]; then
    not_ours "tools/ holds more than uv"
fi
mkdir -p "$D/bin" "$D/releases" "$D/uv"

# One run at a time.
if command -v flock >/dev/null; then
    exec 9>"$D/.lock"
    flock -n 9 || stop "another install is running in $D"
else
    echo "-- no flock here: make sure no other run of this script is going"
fi

work=$(mktemp -d)
new=""
switched=""
cleanup() {
    rm -rf "$work"
    if [ -z "$switched" ]; then
        rm -f "$D/bin/nomad.new" "$D/bin/nomad.before.new" "$D/check_current.sh.new" "$D/current.new"
        if [ -n "$new" ]; then rm -rf "${D:?}/releases/$new"; fi
    fi
}
trap cleanup EXIT
export UV_CACHE_DIR="$work/uv-cache"

# uv, from its release on GitHub, checked against the checksum above.
UV="$D/uv/uv-$UVV"
if [ ! -x "$UV" ]; then
    command -v curl >/dev/null || stop "no curl here"
    case "$(uname -m)" in
        x86_64) arch=x86_64; want=$UV_SHA256_x86_64 ;;
        aarch64) arch=aarch64; want=$UV_SHA256_aarch64 ;;
        *) stop "no uv build for $(uname -m)" ;;
    esac
    echo "-- uv $UVV from GitHub"
    curl -fsSL -o "$work/uv.tar.gz" \
        "https://github.com/astral-sh/uv/releases/download/$UVV/uv-$arch-unknown-linux-gnu.tar.gz" </dev/null
    got=$(sha256sum "$work/uv.tar.gz" | cut -d' ' -f1)
    if [ "$got" != "$want" ]; then stop "uv's download doesn't match its checksum"; fi
    tar -xzf "$work/uv.tar.gz" -C "$work"
    install -m 755 "$work/uv-$arch-unknown-linux-gnu/uv" "$UV.new"
    mv -f "$UV.new" "$UV"
fi
"$UV" --version </dev/null

# This release, whole in its own folder, checked before anyone uses it.
new="$V"
if [ -e "$D/releases/$new" ]; then new="$V-$(date +%Y%m%d%H%M%S)"; fi
R="$D/releases/$new"
mkdir -p "$R"
export UV_PYTHON_INSTALL_DIR="$R/python"
echo "-- building releases/$new"
"$UV" python install --no-bin --quiet 3.12 </dev/null
"$UV" venv --quiet --python 3.12 "$R/venv" </dev/null
if ! grep -q "^home = $R/python/" "$R/venv/pyvenv.cfg"; then
    stop "the new environment isn't on the Python under $R/python"
fi
"$UV" pip install --quiet --compile-bytecode --python "$R/venv/bin/python" "nomad-hpc==$V" </dev/null
chmod -R go-w "$R"
chmod -R a+rX "$R"
if [ -n "$(find "$R" -not -type l -perm /022 -print -quit)" ]; then stop "$R: still writable by others"; fi
if [ -n "$(find "$R" -not -user "$me" -print -quit)" ]; then stop "$R: owned by others than $me"; fi
if ! out=$(PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent "$R/venv/bin/python" -I -B \
        -c 'import sys; sys.argv[0] = "nomad"; from nomad.cli import main; sys.exit(main())' \
        size --help </dev/null 2>&1); then
    echo "$out" | tail -5 >&2
    stop "nomad-hpc $V doesn't start in releases/$new (users keep the one they have)"
fi

# The command users run, and the weekly check: written beside, checked.
cat > "$D/bin/nomad.new" <<EOF
#!/bin/sh
# nomad for users ($V). Its Python runs isolated (-I): PYTHONHOME, PYTHONPATH
# and the like, set here for another Python, are ignored by nomad alone --
# the jobs it submits get the environment as it is.
exec "$R/venv/bin/python" -I -B -c 'import sys; sys.argv[0] = "nomad"; from nomad.cli import main; sys.exit(main())' "\$@"
EOF
upgrade="ssh \$(id -un)@\$(hostname -s) 'bash -s \$latest' < scripts/install_nomad_for_users.sh"
if [ "$D" != "$DEFAULT_D" ]; then
    upgrade="ssh \$(id -un)@\$(hostname -s) 'NOMAD_USERS_DIR=$D bash -s \$latest' < scripts/install_nomad_for_users.sh"
fi
cat > "$D/check_current.sh.new" <<EOF
#!/bin/sh
# Is nomad for users here nomad's latest release? Says so only when a newer
# one is out. For the owner's crontab, e.g. weekly:  17 7 * * 1 $D/check_current.sh
PATH=/usr/bin:/bin:\$PATH
py="$D/current/venv/bin/python"
have=\$("$D/bin/nomad" --version </dev/null 2>/dev/null | awk '{print \$NF}')
if [ -z "\$have" ]; then
    echo "\$(hostname -s): $D/bin/nomad doesn't start"
    exit 0
fi
latest=\$(curl -fsS --max-time 60 https://pypi.org/pypi/nomad-hpc/json </dev/null 2>/dev/null \\
    | "\$py" -I -c 'import json, sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
if [ -z "\$latest" ]; then
    echo "\$(hostname -s): PyPI couldn't be asked for nomad-hpc's latest release (here: \$have)"
    exit 0
fi
newer=\$("\$py" -I -c '
import re, sys
def v(s):
    m = re.match(r"([0-9]+(?:[.][0-9]+)*)(?:(a|b|rc)([0-9]+))?(?:[.]post([0-9]+))?", s)
    if not m:
        return None
    rel = tuple(int(x) for x in m.group(1).split("."))
    rel = rel + (0,) * (4 - len(rel))
    return rel + ((0, int(m.group(3))) if m.group(2) else (1, 0)) + (int(m.group(4) or 0),)
have, latest = v(sys.argv[1]), v(sys.argv[2])
print("yes" if have and latest and latest > have else "")' "\$have" "\$latest" 2>/dev/null)
if [ -n "\$newer" ]; then
    echo "\$(hostname -s): nomad for users is \$have, and nomad-hpc \$latest is out. To upgrade, from a nomad checkout:"
    echo "    $upgrade"
fi
EOF
chmod 755 "$D/bin/nomad.new" "$D/check_current.sh.new"
if ! out=$(PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent "$D/bin/nomad.new" --version </dev/null 2>&1); then
    echo "$out" | tail -5 >&2
    stop "the new bin/nomad doesn't start (users keep the one they have)"
fi

# Switch: bin/nomad is replaced in one step; then current and the check.
# The release before is the one bin/nomad runs now (not what current says).
before=""
if [ -f "$D/bin/nomad" ]; then
    before=$(sed -n "s|^exec \"$D/releases/\([^/]*\)/venv/bin/python\".*|\1|p" "$D/bin/nomad" | head -1 || true)
fi
if [ -n "$before" ] && [ ! -x "$D/releases/$before/venv/bin/python" ]; then before=""; fi
if [ -n "$before" ]; then cp -p "$D/bin/nomad" "$D/bin/nomad.before.new"; fi
switched=yes
mv -f "$D/bin/nomad.new" "$D/bin/nomad"
ln -sfn "releases/$new" "$D/current.new"
mv -Tf "$D/current.new" "$D/current"
mv -f "$D/check_current.sh.new" "$D/check_current.sh"
if [ -f "$D/bin/nomad.before.new" ]; then mv -f "$D/bin/nomad.before.new" "$D/bin/nomad.before"; fi
if [ -z "$before" ]; then rm -f "$D/bin/nomad.before"; fi

# Kept: this release and the one before. Removed: older ones, other uv
# versions, and an install made by 1.18.0's script (venv/, python/, tools/,
# and the python3.12 link it left in this account's ~/.local/bin). Folders
# are first renamed into .nomad-removed (a rename works with files still
# open), then removed; what can't be removed yet (a file open on NFS) is
# said, and the next run removes it. Users already run the new release.
gone() { rm -rf "$1" 2>/dev/null || echo "! couldn't remove all of $1 now (in use?): the next run will"; }
aside="$D/.nomad-removed/$(date +%Y%m%d%H%M%S)"
put_aside() { mkdir -p "$aside" && mv "$1" "$aside/$2" || echo "! couldn't move $1 aside: the next run will"; }
for old in "$D"/releases/*; do
    [ -e "$old" ] || continue
    b=$(basename "$old")
    if [ "$b" != "$new" ] && [ "$b" != "$before" ]; then put_aside "$old" "release-$b"; fi
done
left=""
for x in venv python tools; do
    if [ -e "$D/$x" ]; then put_aside "$D/$x" "$x"; left="$left $x/"; fi
done
for u in "$D"/uv/uv-*; do
    if [ -e "$u" ] && [ "$u" != "$UV" ]; then gone "$u"; fi
done
link="$HOME/.local/bin/python3.12"
if [ -L "$link" ]; then
    case "$(readlink "$link")" in "$D"/python/*|"$given"/python/*) rm -f "$link" || true ;; esac
fi
if [ -e "$D/.nomad-removed" ]; then gone "$D/.nomad-removed"; fi

# Nothing writable but by the owner, nor owned by anyone else.
chmod go-w "$D" "$D/bin" "$D/releases" "$D/uv" "$D/bin/nomad" "$D/check_current.sh"
chmod a+rX "$D" "$D/bin" "$D/releases" "$D/uv" "$D/bin/nomad" "$D/check_current.sh"
back=""
if [ -n "$before" ] && [ "$before" != "$new" ]; then
    back="mv -f $D/bin/nomad.before $D/bin/nomad && ln -sfn releases/$before $D/current.new && mv -Tf $D/current.new $D/current"
fi
problem=""
loose=$(find "$D" -not -type l -perm /022 2>/dev/null | head -5 || true)
if [ -n "$loose" ]; then problem="still writable by others:
$loose"; fi
theirs=$(find "$D" -not -user "$me" 2>/dev/null | head -5 || true)
if [ -n "$theirs" ]; then problem="$problem
owned by others than $me:
$theirs"; fi
if ! out=$(PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent "$D/bin/nomad" --version </dev/null 2>&1); then
    problem="$problem
$D/bin/nomad doesn't start: $(echo "$out" | tail -2)"
fi
if [ -n "$problem" ]; then
    echo "$problem" >&2
    if [ -n "$back" ]; then echo "To go back to the one before: $back" >&2; fi
    stop "fix this before telling users"
fi
up=$(dirname "$D")
while :; do
    if [ -n "$(find "$up" -maxdepth 0 -perm /022 ! -perm -1000 2>/dev/null)" ]; then
        echo "! $up is writable by others than its owner: whoever can write there could replace $D"
    fi
    [ "$up" = / ] && break
    up=$(dirname "$up")
done
echo "== $(hostname -s): $out (deaf to PYTHONHOME and PYTHONPATH)"
if [ -n "$left" ]; then
    if [ -e "$D/.nomad-removed" ]; then left="; the 1.18.0 layout set aside in .nomad-removed:$left (removed next run)"
    else left="; removed the 1.18.0 layout:$left"; fi
fi
echo "   $D: $(du -sh "$D" 2>/dev/null | cut -f1), owner $me; releases/$new${before:+ (kept: releases/$before)}$left"

cat <<EOF

Users need $D/bin on their PATH (it holds only nomad). Either:
  - a link in a folder already on it, e.g.
        ln -s $D/bin/nomad /usr/local/sw/bin/nomad
  - or a module, e.g. a modulefile with
        append-path PATH $D/bin

To hear when a newer release is out, in this account's crontab (crontab -e):
    MAILTO=you@your.edu
    17 7 * * 1 $D/check_current.sh
EOF
if [ -n "$back" ]; then
    cat <<EOF

To go back to the one before (releases/$before):
    $back
EOF
fi
