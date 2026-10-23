#!/bin/sh
# nomad-storage.sh -- who uses a storage server's space, for NØMAÐ
# (`nomad storage script` prints this file; see docs/storage.md).
#
# Runs from cron (or a systemd timer) on the storage server itself: a Linux
# file server (XFS, ext4, ...) or a TrueNAS box (FreeBSD or Linux, ZFS). It
# only reads: df, xfs_quota report, zfs list / get / userspace / groupspace,
# find and stat. It writes its findings to DIR (default
# /var/lib/nomad-storage), readable by its owner and GROUP, where nomad's
# storage_use collector picks them up with an ordinary account. nomad itself
# never runs with privileges here.
#
# Seeing everyone's files takes root, sudo, or an account given just those
# rights (`nomad storage cron --run-as root|sudo|sudo-readers|user` prints
# each set-up). With -S it runs as an ordinary account and asks sudo -n for
# just the readers that need root: xfs_quota and zfs for usage, GNU du for
# the scan (where sudo already allows those, as on many file servers).
# Whoever runs it, the files say who did and what it couldn't read.
#
#   sh nomad-storage.sh [-d DIR] [-g GROUP] [-S] usage PATH...
#       Each owner's space on each PATH, from the filesystem's own accounting
#       (ZFS always has it; XFS when user or group accounting is on): fast,
#       run it every hour or few. Quotas are included where set.
#   sh nomad-storage.sh [-d DIR] [-g GROUP] [-S] [-s SECONDS] [-a DAYS,DAYS] scan PATH...
#       The folders one level inside each folder of PATH (/home/USER/FOLDER):
#       size, files, the newest change, and how much is older than the two
#       ages (180 and 365 days). Run nightly: it works at the lowest priority,
#       for at most SECONDS (10800) in all, the folders tried longest ago
#       first, and the next run goes on from there. A folder of PATH that is
#       itself another of the PATHs given (symbolic links resolved) is left
#       to that one, and a PATH given twice is scanned once; one that holds
#       another PATH deeper down is said (#contains), as that PATH's files
#       are counted in both.
#   sh nomad-storage.sh [-d DIR] [-g GROUP] -O [-L GB] [-U UID] [-F FIND] [-s SECONDS] [-a DAYS,DAYS] scan PATH...
#       The same, for an account that may act as the others through sudo but
#       isn't root: each top folder measured as its owner (sudo -n -u OWNER
#       find, read only, one find per owner for all its folders), each weekly
#       (on its own night), nightly when it is GB (10) or larger or changed
#       (a plain stat of it). The others keep their last measurement. Owners
#       it can't act as are listed as not measured: root and accounts numbered
#       below UID (1000) without asking sudo, and one sudo refused, asked
#       again only after a week. With -F, sudo runs the wrapper FIND
#       (nomad-storage-find, root's) rather than find itself, so sudoers
#       need allow only it. GNU find (Linux).
#
# DIR is made by the script (mode 750, with a .nomad-storage file in it); an
# existing folder it didn't make is refused, so that a mistyped -d can't
# change the permissions of a pool or a home. DIR, the script and every
# folder above them must be changeable only by root (or the account running
# it): anyone else could swap them for something else. One run of each kind
# at a time.
#
# Lines: '#key<TAB>values' for what the run is and what it read, data lines
# under them. Names are as this server resolves them; a number when it can't.

# Its own PATH, never the caller's (under sudo, the caller's would choose
# what root runs); with the sbin folders, where xfs_quota and zfs live.
PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin
export PATH

NOMAD_STORAGE_VERSION=1
DIR=/var/lib/nomad-storage
GROUP=
SUDO=
OWNERS=
LARGE=10
MINUID=1000
FINDW=
BUDGET=10800
AGES=180,365
NL='
'
TAB=$(printf '\t')

die() { printf 'nomad-storage: %s\n' "$*" >&2; exit 2; }

while getopts d:g:s:a:L:U:F:SO opt; do
    case $opt in
        d) DIR=$OPTARG ;;
        g) GROUP=$OPTARG ;;
        S) SUDO=1 ;;
        O) OWNERS=1 ;;
        L) LARGE=$OPTARG ;;
        U) MINUID=$OPTARG ;;
        F) FINDW=$OPTARG ;;
        s) BUDGET=$OPTARG ;;
        a) AGES=$OPTARG ;;
        *) die "usage: sh nomad-storage.sh [-d DIR] [-g GROUP] [-S | -O [-L GB] [-U UID] [-F FIND]] [-s SECONDS] [-a DAYS,DAYS] usage|scan PATH..." ;;
    esac
done
shift $((OPTIND - 1))
MODE=${1:-}
[ $# -gt 0 ] && shift
case $MODE in usage|scan) ;; *) die "say usage or scan, then the paths" ;; esac
[ $# -gt 0 ] || die "no path given"
case $DIR in /*) ;; *) die "-d: an absolute folder" ;; esac
case $BUDGET in ''|*[!0-9]*) die "-s: a number of seconds" ;; esac
case $LARGE in ''|*[!0-9]*) die "-L: a number of GB" ;; esac
case $MINUID in ''|*[!0-9]*) die "-U: an account number" ;; esac
if [ -n "$OWNERS" ]; then
    [ "$MODE" = scan ] || die "-O: for the scan only"
    [ -z "$SUDO" ] || die "-O and -S: one or the other"
fi
AGE1=${AGES%%,*}
AGE2=${AGES#*,}
case "$AGE1$AGE2" in ''|*[!0-9]*) die "-a: two numbers of days, as 180,365" ;; esac
for p in "$DIR" "$@"; do
    case $p in *"$NL"*|*"$TAB"*|*\\*) die "a path with a tab, line break or backslash" ;; esac
done
for p in "$@"; do
    case $p in /*) ;; *) die "$p: an absolute path" ;; esac
    [ -d "$p" ] || die "$p: no such folder"
done

has() { command -v "$1" >/dev/null 2>&1; }
now() { date +%s; }
# A name as one field on one line: control characters (tabs, line breaks)
# become '?'.
clean() { printf '%s' "$1" | LC_ALL=C tr '\001-\037\177' '[?*]'; }
# The readers that need root: through sudo -n with -S (by full path, as
# sudoers names them).
RUN=
[ -n "$SUDO" ] && RUN="sudo -n"
XQ=$(command -v xfs_quota 2>/dev/null)
ZFS=$(command -v zfs 2>/dev/null)
DU=$(command -v du 2>/dev/null)
if [ -n "$SUDO" ] && [ "$MODE" = scan ] && ! du --version 2>/dev/null | grep -q GNU; then
    die "-S: the scan through sudo needs GNU du (Linux); run it as root here"
fi
# A path without its trailing slash.
norm() { case $1 in /) printf '/' ;; */) printf '%s' "${1%/}" ;; *) printf '%s' "$1" ;; esac; }
# The first line a command said on stderr, as one field.
said() { [ -s "$1" ] && clean "$(head -n 1 "$1")"; }

ME=$(id -u)

ME_NAME=$(id -un 2>/dev/null)
BAD=
WHY=
# Whether only root or this account can change $1: owned by one of them,
# writable by no one else, and without an access list (ACL) that could let
# someone else write it. A sticky folder (as /tmp) is fine: others can't
# rename or remove what's in it. WHY says what is wrong, and how to fix it.
only_us() {
    # (ls of one path: its owner and its ACL mark, the same on GNU and BSD.)
    # shellcheck disable=SC2012
    if [ -z "$(find "$1" -maxdepth 0 \( -user 0 -o -user "$ME" \) 2>/dev/null)" ]; then
        WHY="it belongs to another account (uid $(ls -ldn "$1" | awk '{ print $3 }')): chown it to $ME_NAME or root, or use another folder"
        return 1
    fi
    # shellcheck disable=SC2012
    case $(ls -ld "$1" | cut -c11) in
        +) WHY="it has an access list (ACL) that may let others write it: remove its entries (setfacl -b, or on TrueNAS strip the ACL), or use another folder"
           return 1 ;;
    esac
    [ -n "$(find "$1" -maxdepth 0 -type d -perm -1000 2>/dev/null)" ] && return 0
    if [ -n "$(find "$1" -maxdepth 0 \( -perm -0020 -o -perm -0002 \) 2>/dev/null)" ]; then
        WHY="its group or everyone may write it: chmod g-w,o-w $1, or use another folder"
        return 1
    fi
    return 0
}
# Whether every folder from / down to $1 (included) is so; BAD names the
# first that isn't.
safe() {
    q=$1
    while :; do
        only_us "$q" || { BAD=$q; return 1; }
        [ "$q" = / ] && return 0
        q=$(dirname "$q")
    done
}
# What to say of BAD.
changeable() {
    printf '%s can be changed by accounts other than %s%s: %s' \
        "$BAD" "$ME_NAME" "$([ "$ME" = 0 ] || printf ' and root')" "$WHY"
}
# A folder as it really is, symbolic links resolved.
real() { (cd -P -- "$1" 2>/dev/null && pwd -P); }

# -- the script itself ------------------------------------------------------------------
# Whoever can change it can make it do anything its runner can.
if [ -f "$0" ]; then
    if ! only_us "$0"; then
        BAD=$0
        die "the script: $(changeable)"
    fi
    if ! { sdir=$(real "$(dirname "$0")") && safe "$sdir"; }; then
        die "the script's folder: $(changeable)"
    fi
fi

# -- the output folder ----------------------------------------------------------------
[ -L "$DIR" ] && die "$DIR is a symbolic link: give the folder itself"
if [ ! -e "$DIR" ]; then
    up=$(dirname "$DIR")
    while [ ! -e "$up" ]; do up=$(dirname "$up"); done
    if ! { upr=$(real "$up") && safe "$upr"; }; then
        die "-d $DIR: $(changeable)"
    fi
    mkdir -p "$DIR" || die "can't make $DIR"
    chmod 750 "$DIR" || die "can't set the permissions of $DIR"
    : > "$DIR/.nomad-storage" || die "can't write in $DIR"
fi
[ -d "$DIR" ] || die "$DIR isn't a folder"
# From here on, only names inside DIR, from inside it: nothing can be swapped
# under a path once the folders above it are known to be safe.
cd -P -- "$DIR" || die "can't go into $DIR"
HERE=$(pwd -P)
[ "$HERE" = / ] || safe "$(dirname "$HERE")" || die "-d $DIR: $(changeable)"
[ -n "$(find . -maxdepth 0 -user "$ME" 2>/dev/null)" ] || die "$DIR isn't owned by the account running this"
[ -f .nomad-storage ] && [ ! -L .nomad-storage ] \
    || die "$DIR wasn't made by nomad-storage.sh: give a new folder (or, to use this one, create $DIR/.nomad-storage)"
chmod 750 . || die "can't set the permissions of $DIR (on TrueNAS, a dataset whose ACL mode allows chmod)"
if [ -n "$GROUP" ]; then chgrp "$GROUP" . || die "no group $GROUP"; fi
umask 027

LOCK=
TMP=
CAND=
LIST=
ERRF=
STATS=
STATE=scan.state
cleanup() {
    [ -n "$TMP" ] && rm -f "$TMP" "$TMP.ds"
    [ -n "$ERRF" ] && rm -f "$ERRF"
    [ -n "$CAND" ] && rm -f "$CAND" "$LIST" "$STATE.n"
    [ -n "$STATS" ] && rm -f "$STATS" "$DUE" "$OWN" "$AGG" "$WHO"
    [ -n "$LOCK" ] && rm -rf "$LOCK"
}
trap cleanup EXIT
trap 'cleanup; trap - EXIT; exit 1' INT TERM HUP

# One run of each kind at a time: a scan still going when the next starts is
# left to finish. A lock is stale when its run is gone, or older than any run
# can be (its number may since belong to another process).
L=".lock-$MODE"
if ! mkdir "$L" 2>/dev/null; then
    limit=60
    [ "$MODE" = scan ] && limit=$((BUDGET / 60 + 60))
    old=$(cat "$L/pid" 2>/dev/null)
    if [ -n "$(find "$L" -maxdepth 0 -mmin +"$limit" 2>/dev/null)" ]; then
        :
    elif [ -z "$old" ]; then
        # Being made this moment, or by a run that died before writing its pid.
        if [ -z "$(find "$L" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
            printf 'nomad-storage: another %s run is starting\n' "$MODE" >&2
            exit 0
        fi
    elif kill -0 "$old" 2>/dev/null; then
        printf 'nomad-storage: the %s run started earlier (pid %s) is still going\n' "$MODE" "$old" >&2
        exit 0
    fi
    rm -rf "$L"
    mkdir "$L" || die "can't lock $DIR/$L"
fi
LOCK=$L
echo $$ > "$LOCK/pid"

OUT="$MODE.txt"
TMP="$OUT.tmp.$$"
ERRF="$MODE.err.$$"
: > "$TMP" || die "can't write in $DIR"

emit() { printf '%s\n' "$*" >> "$TMP"; }
started=$(now)
emit "#nomad-storage	$NOMAD_STORAGE_VERSION	$MODE"
emit "#host	$(clean "$(hostname)")"
emit "#os	$(clean "$(uname -sr)")"
emit "#run-by	$ME	$(clean "$(id -un 2>/dev/null)")	$(clean "${SUDO_USER:--}")"
[ -n "$SUDO" ] && emit "#readers	sudo"
emit "#started	$started"

# -- usage ------------------------------------------------------------------------------

# What a path is: zfs (and its dataset), else the kernel's name for it.
fstype() {
    if [ -n "$ZFS" ] && ds=$("$ZFS" list -H -o name "$1" 2>/dev/null) && [ -n "$ds" ]; then
        printf 'zfs\t%s\n' "$ds"
    elif t=$(stat -f -c %T "$1" 2>/dev/null) && [ -n "$t" ]; then
        printf '%s\t-\n' "$t"
    else
        printf 'other\t-\n'
    fi
}

usage_of() {
    p=$(norm "$1")
    info=$(fstype "$p")
    kind=${info%%"$TAB"*}
    ds=${info#*"$TAB"}
    line=$(df -P -k "$p" 2>/dev/null | tail -n 1)
    # The mount point: what follows the first five fields (it may hold spaces).
    mnt=$(printf '%s\n' "$line" | awk '{ for (i = 1; i <= 5; i++) sub(/^[^ ]+ +/, ""); print }')
    emit "#path	$p	$kind	$ds"
    emit "#df	$(printf '%s' "$line" | tr -s ' ' '	')"
    case $kind in
        zfs)
            # The dataset holding the path, and those below it mounted under
            # the path; others mounted elsewhere are theirs, not this path's.
            dsmnt=$("$ZFS" get -H -o value mountpoint "$ds" 2>/dev/null)
            [ "$dsmnt" = "$p" ] || emit "#covers	$(clean "$dsmnt")"
            emit "#datasets"
            "$ZFS" list -H -p -o name,mountpoint,used,available,quota,refquota,referenced \
                -r -t filesystem "$ds" 2>/dev/null \
                | NOMAD_P="$p" NOMAD_DS="$ds" awk -F '\t' '
                    $1 == ENVIRON["NOMAD_DS"] || $2 == ENVIRON["NOMAD_P"] ||
                    index($2, ENVIRON["NOMAD_P"] "/") == 1' > "$TMP.ds"
            cat "$TMP.ds" >> "$TMP"
            cut -f 1 "$TMP.ds" | while IFS= read -r d; do
                emit "#zfs-users	$d"
                # shellcheck disable=SC2086
                $RUN "$ZFS" userspace -H -p -o type,name,used,quota "$d" < /dev/null 2>"$ERRF" >> "$TMP" \
                    || emit "#error	zfs userspace $d: $(said "$ERRF")"
                emit "#zfs-groups	$d"
                # shellcheck disable=SC2086
                $RUN "$ZFS" groupspace -H -p -o type,name,used,quota "$d" < /dev/null 2>"$ERRF" >> "$TMP" \
                    || emit "#error	zfs groupspace $d: $(said "$ERRF")"
            done
            rm -f "$TMP.ds"
            ;;
        xfs)
            if [ -n "$XQ" ]; then
                # Blocks of 1 KiB: used, soft, hard (0: no limit). The counts
                # are the whole filesystem's, at its mount point.
                [ "$mnt" = "$p" ] || emit "#covers	$(clean "$mnt")"
                # shellcheck disable=SC2086
                u=$($RUN "$XQ" -x -c 'report -u -b -N' "$mnt" 2>"$ERRF" < /dev/null)
                why=$(said "$ERRF")
                # shellcheck disable=SC2086
                g=$($RUN "$XQ" -x -c 'report -g -b -N' "$mnt" 2>/dev/null < /dev/null)
                # shellcheck disable=SC2086
                j=$($RUN "$XQ" -x -c 'report -p -b -N' "$mnt" 2>/dev/null < /dev/null)
                if [ -n "$u$g$j" ]; then
                    emit "#xfs-users"
                    [ -n "$u" ] && emit "$u"
                    emit "#xfs-groups"
                    [ -n "$g" ] && emit "$g"
                    emit "#xfs-projects"
                    [ -n "$j" ] && emit "$j"
                elif [ -n "$SUDO" ]; then
                    emit "#no-accounting	xfs_quota through sudo -n said nothing (is $XQ allowed? sudo -l)${why:+: $why}"
                elif [ "$ME" != 0 ]; then
                    emit "#no-accounting	xfs_quota's report needs root here (run usage as root, through sudo, or with -S)${why:+: $why}"
                else
                    emit "#no-accounting	XFS user and group accounting are off here (the uquota/gquota or uqnoenforce/gqnoenforce mount options): the scan counts by folder"
                fi
            else
                emit "#no-accounting	xfs_quota is not installed: the scan counts by folder"
            fi
            ;;
        *)
            emit "#no-accounting	$kind keeps no per-owner counts that this reads: the scan counts by folder"
            ;;
    esac
}

# -- scan -------------------------------------------------------------------------------

# One top-level folder's folders: on stdin, a D line per folder (owner, name),
# a line per file (512-byte blocks, mtime, links, inode, name below the top),
# an N line per file whose path holds a line break (counted as folder "/",
# which no real folder can be called), then '__rc N'. Out: an F line per
# folder ("." for files directly in the top): owner, bytes, files, newest
# change, bytes older than AGE1 and AGE2. A file with several links counts
# once in its top folder. Other control characters in names reach it as '?'.
# shellcheck disable=SC2016
SCAN_AWK='
function add(key, blocks, mtime, links, inode,    b) {
    if (links > 1) { if (inode in hard) return; hard[inode] = 1 }
    b = blocks * 512; bytes[key] += b; files[key]++; seen[key] = 1
    if (mtime > newest[key]) newest[key] = mtime
    if (mtime < cut1) old1[key] += b
    if (mtime < cut2) old2[key] += b
}
BEGIN { pre = ENVIRON["NOMAD_PRE"] }
/^__rc / { rc = $2; next }
$1 == "D" { line = $0; sub(/^D [0-9]+ /, "", line); owner[line] = $2; seen[line] = 1; next }
$1 == "N" && NF >= 5 { add("/", $2, int($3), $4, $5); next }
/^[0-9]+ -?[0-9.]+ [0-9]+ [0-9]+ / {
    name = $0; for (i = 1; i <= 4; i++) sub(/^[^ ]+ /, "", name)
    if (pre != "" && index(name, pre) == 1) name = substr(name, length(pre) + 1)
    slash = index(name, "/"); key = slash ? substr(name, 1, slash - 1) : "."
    add(key, $1, int($2), $3, $4)
}
END {
    for (k in seen) {
        if (!(k in owner)) owner[k] = "-"
        printf "F\t%s\t%s\t%.0f\t%.0f\t%.0f\t%.0f\t%.0f\n", k, owner[k], bytes[k], files[k], newest[k], old1[k], old2[k]
    }
    printf "#rc\t%s\n", rc
}'

# With -S, the scan reads through GNU du run by sudo: each file's size (KiB)
# and mtime, each folder after what is in it (NUL-ended, so a name with a
# line break stays one record). A folder with anything in it comes right
# after the last thing in it: it is left out, its size being everything
# below it; an empty one counts as a file. A file with several links is
# listed once. To SCAN_AWK's lines (512-byte blocks, mtime, 1 link).
# shellcheck disable=SC2016
DU_AWK='
BEGIN { FS = "\t"; top = ENVIRON["NOMAD_TOP"]; pre = top "/" }
/^__rc / { print; next }
$1 ~ /^[0-9]+$/ && $2 ~ /^-?[0-9]+$/ {
    name = substr($0, length($1) + length($2) + 3)
    if (prev != "" && index(prev, name "/") == 1) { prev = name; next }
    prev = name
    if (index(name, pre) != 1) next
    print ($1 * 2) " " $2 " 1 0 " substr(name, length(pre) + 1)
}'

# With -O, one owner's top folders (NOMAD_TOPS, one a line, in the order
# given to find) from one find run as the owner: a D line per folder (depth,
# owner, path; depth 0 is a top folder itself: find has reached it, and what
# follows is in it until the next), an L line per file (512-byte blocks,
# mtime, links, inode, path), an N line per file whose path holds a line
# break (its top folder in place of the path), an X line after a folder the
# owner can't list or enter, then '__rc N'. A folder whose path holds a line
# break prints no D line, so no line can be forged; and the numbers are
# checked. Out: '__top I REACHED' for each top folder, '__denied I N' (its
# folders the owner couldn't read), '__last I' (the last one reached), an F
# line per folder with the top folder's number after the F (as SCAN_AWK's
# otherwise), and '#rc N'.
# shellcheck disable=SC2016
OWNER_AWK='
function add(t, key, blocks, mtime, links, inode,    b, k) {
    if (links > 1) { if ((t, inode) in hard) return; hard[t, inode] = 1 }
    k = t SUBSEP key
    b = blocks * 512; bytes[k] += b; files[k]++; seen[k] = 1
    if (mtime > newest[k]) newest[k] = mtime
    if (mtime < cut1) old1[k] += b
    if (mtime < cut2) old2[k] += b
}
function num(x) { return x ~ /^[0-9]+$/ }
function when(x) { return x ~ /^-?[0-9]+(\.[0-9]+)?$/ }
function rest(line, skip,    i) { for (i = 1; i <= skip; i++) sub(/^[^ ]+ /, "", line); return line }
# In the top folder find is in (cur), and not that folder itself.
function inside(p) { return cur && index(p, top[cur] "/") == 1 }
BEGIN { n = split(ENVIRON["NOMAD_TOPS"], top, "\n"); while (n > 0 && top[n] == "") n--; cur = 0 }
/^__rc / { rc = $2; next }
$0 == "X" { if (cur) xd[cur]++; next }
$1 == "D" && NF >= 4 && num($2) && num($3) {
    p = rest($0, 3)
    if ($2 == 0) {
        # The next starting point (find takes them in order; one it could
        # not reach is passed over).
        for (i = cur + 1; i <= n && top[i] != p; i++) ;
        if (i <= n) { cur = i; ran[i] = 1 } else cur = 0
        next
    }
    if ($2 == 1 && inside(p)) { name = substr(p, length(top[cur]) + 2); owner[cur, name] = $3; seen[cur SUBSEP name] = 1 }
    next
}
$1 == "N" && NF >= 6 && num($2) && when($3) && num($4) && num($5) {
    if (cur && rest($0, 5) == top[cur]) add(cur, "/", $2, int($3), $4, $5)
    next
}
$1 == "L" && NF >= 6 && num($2) && when($3) && num($4) && num($5) {
    p = rest($0, 5)
    if (!inside(p)) next
    name = substr(p, length(top[cur]) + 2)
    slash = index(name, "/"); key = slash ? substr(name, 1, slash - 1) : "."
    add(cur, key, $2, int($3), $4, $5)
}
END {
    last = 0
    for (i = 1; i <= n; i++) {
        printf "__top\t%d\t%d\n__denied\t%d\t%d\n", i, (i in ran) ? 1 : 0, i, xd[i] + 0
        if (i in ran) last = i
    }
    printf "__last\t%d\n", last
    for (k in seen) {
        split(k, kk, SUBSEP)
        if (!(k in owner)) owner[k] = "-"
        printf "F\t%s\t%s\t%s\t%.0f\t%.0f\t%.0f\t%.0f\t%.0f\n", kk[1], kk[2], owner[k], bytes[k], files[k], newest[k], old1[k], old2[k]
    }
    printf "#rc\t%s\n", rc
}'

# With -O: which top folders to measure tonight. In: scan.state (when each
# was last measured, how, its mtime:uid:mode then, its bytes, its owner's
# uid), the stat of each now (mtime:uid:mode, uid, user, folder), the list
# (oldest first). Out,
# in the list's order: M uid user path folder mtime:uid:mode (measure it), C path
# folder (its last measurement stands), S why uid path folder (not measured,
# sudo not asked: root's, a system account's below MINUID, or an owner sudo
# refused less than a week ago and still its owner). A folder of this
# account is always measured. Due: never finished (done, or denied: all of
# it its owner may read), a week and a night since, large (bytes >= LARGE),
# changed (its mtime, owner or mode: an entry made, removed or renamed in it,
# or a new owner or mode), or its own night of the week (a hash of its path,
# spread over seven).
# shellcheck disable=SC2016
DUE_AWK='
function slot(s,    h, i) { h = 0; for (i = 1; i <= length(s); i++) h = (h * 31 + ord[substr(s, i, 1)]) % 1000003; return h % 7 }
BEGIN { FS = "\t"; for (i = 1; i < 256; i++) ord[sprintf("%c", i)] = i; day = int(now / 86400) % 7 }
FILENAME == ARGV[1] { last[$3] = $1; how[$3] = $2; mt0[$3] = $4; size[$3] = $5; uid0[$3] = $6; next }
FILENAME == ARGV[2] {
    line = $0; m = line; sub(/ .*/, "", m); sub(/^[^ ]+ /, "", line)
    u = line; sub(/ .*/, "", u); sub(/^[^ ]+ /, "", line)
    w = line; sub(/ .*/, "", w); sub(/^[^ ]+ /, "", line)
    mt[line] = m; uid[line] = u; user[line] = w; next
}
{
    t = $3
    if (!(t in uid)) next
    if (uid[t] != me) {
        why = ""
        if (uid[t] == 0) why = "root"
        else if (uid[t] + 0 < minuid + 0) why = "system"
        else if (how[t] == "nosudo" && now - last[t] < 8 * 86400 && uid0[t] == uid[t]) why = "sudo"
        if (why != "") { print "S\t" why "\t" uid[t] "\t" $2 "\t" t; next }
    }
    due = (how[t] != "done" && how[t] != "denied") || now - last[t] >= 8 * 86400 || size[t] + 0 >= large + 0 || mt[t] != mt0[t] ||
          (slot(t) == day && now - last[t] >= 20 * 3600)
    if (due) print "M\t" uid[t] "\t" user[t] "\t" $2 "\t" t "\t" mt[t]
    else print "C\t" $2 "\t" t
}'

if find / -maxdepth 0 -printf '' >/dev/null 2>&1; then GNUFIND=1; else GNUFIND=; fi
[ -z "$OWNERS" ] || [ -n "$GNUFIND" ] || die "-O: needs GNU find (Linux)"
if [ -n "$FINDW" ]; then
    [ -n "$OWNERS" ] || die "-F: with -O only"
    case $FINDW in /*) ;; *) die "-F: the wrapper's absolute path" ;; esac
    [ -f "$FINDW" ] && [ -x "$FINDW" ] || die "-F: no wrapper at $FINDW"
    # sudo runs it as every owner: it, and every folder from / down to it
    # (links followed), must be root's, and only root may change them.
    # (%A: -rwxr-xr-x; the 6th and 9th, group's and others' w, must be -.)
    w=$(readlink -f -- "$FINDW" 2>/dev/null) && [ -n "$w" ] || die "-F: can't resolve $FINDW"
    # Its real path, no link anywhere in it: sudoers names this path, and
    # a link the account could change would let it swap what sudo runs.
    [ "$w" = "$FINDW" ] || die "-F: give the wrapper's real path, $w (no link in it), and name that in sudoers"
    d=$w
    while :; do
        case $(stat -c '%u %A' -- "$d" 2>/dev/null) in
            "0 "?????-??-*) ;;
            *) die "-F: $d must be root's and writable by root only (it holds or is the wrapper)" ;;
        esac
        [ "$d" = / ] && break
        d=$(dirname -- "$d")
    done
    grep -q '^NOMAD_STORAGE_FIND=1$' "$w" 2>/dev/null \
        || die "-F: $FINDW isn't nomad-storage-find (nomad storage script --find)"
fi
FIND=$(command -v find 2>/dev/null)
IONICE=
if has ionice; then IONICE='ionice -c3'; fi

scan_top() {
    top=$1 secs=$2
    t=$(now)
    cut1=$((t - AGE1 * 86400))
    cut2=$((t - AGE2 * 86400))
    {
        if [ -n "$SUDO" ]; then
            # The folders' owners would take root to read: left out ("-").
            {
                # shellcheck disable=SC2086
                timeout -k 10 "$secs" nice -n 19 $IONICE sudo -n "$DU" -x -a -k --time --time-style=+%s -0 \
                    -- "$top" 2>>"$ERRF"
                printf '\000__rc %s\000' "$?"
            } | LC_ALL=C tr '\n\000' '\001\n' | NOMAD_TOP="$top" awk "$DU_AWK"
        elif [ -n "$GNUFIND" ]; then
            find "$top" -mindepth 1 -maxdepth 1 -type d ! -name "*$NL*" -printf 'D %U %P\n' 2>>"$ERRF"
            # shellcheck disable=SC2086
            timeout -k 10 "$secs" nice -n 19 $IONICE find "$top" -xdev -type f \
                \( -path "*$NL*" -printf 'N %b %T@ %n %i\n' -o -printf '%b %T@ %n %i %P\n' \) 2>>"$ERRF"
            printf '__rc %s\n' "$?"
        else
            find "$top" -mindepth 1 -maxdepth 1 -type d ! -name "*$NL*" -exec stat -f 'D %u %N' {} + 2>>"$ERRF" \
                | NOMAD_PRE="$top/" awk '{ s = $0; sub(/^D [0-9]+ /, "", s); p = ENVIRON["NOMAD_PRE"]
                    if (index(s, p) == 1) s = substr(s, length(p) + 1); print "D " $2 " " s }'
            timeout -k 10 "$secs" nice -n 19 find "$top" -xdev -type f \
                \( -path "*$NL*" -exec stat -f 'N %b %m %l %i' {} + \
                   -o -exec stat -f '%b %m %l %i %N' {} + \) 2>>"$ERRF"
            printf '__rc %s\n' "$?"
        fi
    } | LC_ALL=C tr '\001-\011\013-\037\177' '[?*]' \
      | NOMAD_PRE="$([ -n "$GNUFIND$SUDO" ] || printf '%s/' "$top")" \
        nice -n 19 awk -v cut1="$cut1" -v cut2="$cut2" "$SCAN_AWK"
}

# Each top folder of owner $1 (uid) and $2 (name) in OWN: not measured, why $3.
unmeasured() {
    while IFS="$TAB" read -r _m _u _n p top _mt; do
        emit "#not-measured	$p	$(clean "${top#"$p"/}")	$1	$3"
    done < "$OWN"
}

# -O: the due top folders, one find per owner (as the owner, through sudo -n
# -u, unless it is this account), owners in the order their oldest folder
# comes; at the budget the rest are left for the next run.
owner_scan() {
    DUE="scan.due.$$"; OWN="scan.own.$$"; AGG="scan.agg.$$"; WHO="scan.who.$$"; STATS="scan.stats.$$"
    cut -f 3 "$LIST" | tr '\n' '\000' | xargs -0 -r stat -c '%Y:%u:%a %u %U %n' -- 2>/dev/null > "$STATS"
    LC_ALL=C awk -v now="$(now)" -v large="${LARGE}000000000" -v me="$ME" -v minuid="$MINUID" "$DUE_AWK" \
        "$STATE" "$STATS" "$LIST" > "$DUE"
    emit "#carried	$(grep -c '^C' "$DUE")"
    # Not measured, and sudo not asked (a refusal's line stays in the state
    # until it is asked again).
    while IFS="$TAB" read -r kind why ouid p top; do
        [ "$kind" = S ] || continue
        emit "#not-measured	$p	$(clean "${top#"$p"/}")	$ouid	$why"
    done < "$DUE"
    awk -F '\t' '$1 == "M" && !seen[$2]++ { print $2 "\t" $3 }' "$DUE" > "$WHO"
    while IFS="$TAB" read -r ouid oname; do
        awk -F '\t' -v u="$ouid" '$1 == "M" && $2 == u' "$DUE" > "$OWN"
        remaining=$((BUDGET - $(now) + started))
        if [ "$remaining" -lt 60 ]; then left=$((left + $(wc -l < "$OWN"))); continue; fi
        # A name sudo takes as a name and nothing else.
        case $oname in
            ''|UNKNOWN|-*|*[!A-Za-z0-9._-]*) unmeasured "$ouid" "$oname" name; continue ;;
        esac
        # By number, so it is exactly the folder's owner (a name could be
        # another account's in a muddled directory).
        AS=
        [ "$ouid" = "$ME" ] || AS="sudo -n -u #$ouid --"
        tops=$(cut -f 5 "$OWN" | LC_ALL=C tr '\001-\011\013-\037\177' '[?*]')
        set --
        while IFS="$TAB" read -r _m _u _n _p top _mt; do set -- "$@" "$top"; done < "$OWN"
        t0=$(now)
        cut1=$((t0 - AGE1 * 86400))
        cut2=$((t0 - AGE2 * 86400))
        : > "$ERRF"
        {
            # From /, which every owner may enter (this folder they may not).
            # With -F, the wrapper runs this same find, and sudo allows only it.
            if [ -n "$FINDW" ]; then
                # shellcheck disable=SC2086
                (cd / && exec timeout -k 10 "$remaining" nice -n 19 $IONICE $AS "$FINDW" "$@") 2>>"$ERRF" < /dev/null
            else
                # shellcheck disable=SC2086
                (cd / && exec timeout -k 10 "$remaining" nice -n 19 $IONICE $AS "$FIND" "$@" -xdev \
                    \( -type d \( -path "*$NL*" -o -printf 'D %d %U %p\n' \) \( -readable -executable -o -printf 'X\n' \) \) -o \
                    \( -type f \( -path "*$NL*" -printf 'N %b %T@ %n %i %H\n' -o -printf 'L %b %T@ %n %i %p\n' \) \)) \
                    2>>"$ERRF" < /dev/null
            fi
            printf '__rc %s\n' "$?"
        } | LC_ALL=C tr '\001-\011\013-\037\177' '[?*]' \
          | NOMAD_TOPS="$tops" nice -n 19 awk -v cut1="$cut1" -v cut2="$cut2" "$OWNER_AWK" > "$AGG"
        secs=$(( $(now) - t0 ))
        rc=$(sed -n 's/^#rc	//p' "$AGG")
        # It reached none of them: sudo wouldn't act as the owner. Not asked
        # again for a week (sudo logs each refusal), unless the owner changes.
        if ! grep -q '^__top	[0-9]*	1$' "$AGG"; then
            if [ -n "$AS" ]; then
                unmeasured "$ouid" "$oname" sudo
                while IFS="$TAB" read -r _m _u _n _p top mt; do
                    printf '%s\tnosudo\t%s\t%s\t0\t%s\n' "$t0" "$top" "$mt" "$ouid" >> "$STATE"
                done < "$OWN"
            else
                unmeasured "$ouid" "$oname" unreadable
            fi
            continue
        fi
        # Each top folder: done when find finished (0, or 1: some files
        # vanished or couldn't be read), or moved on past it; the one it was
        # in when the time ran out is partial; those it never reached are
        # left for the next run (or, when it finished, were gone).
        last=$(sed -n 's/^__last	//p' "$AGG")
        i=0
        while IFS="$TAB" read -r _m _u _n p top mt; do
            i=$((i + 1))
            if ! grep -q "^__top	$i	1\$" "$AGG"; then
                case $rc in
                    0|1) emit "#not-measured	$p	$(clean "${top#"$p"/}")	$ouid	unreachable" ;;
                    *) left=$((left + 1)) ;;
                esac
                continue
            fi
            denied=$(sed -n "s/^__denied	$i	//p" "$AGG")
            denied=${denied:-0}
            case $rc in
                0|1) how="done"; trc=$rc ;;
                *) if [ "$i" -lt "$last" ]; then how="done"; trc=0; else how="partial"; trc=$rc; fi ;;
            esac
            # As its owner, what it couldn't read is the owner's own doing
            # (a folder made unreadable): the figure is short, and doesn't
            # replace a whole one.
            if [ "$how" = "done" ] && [ "$denied" -gt 0 ]; then how="denied"; trc=1; fi
            emit "#top	$p	$(clean "${top#"$p"/}")	$ouid	$t0"
            awk -F '\t' -v i="$i" '$1 == "F" && $2 == i { printf "F"; for (j = 3; j <= NF; j++) printf "\t%s", $j; printf "\n" }' \
                "$AGG" >> "$TMP"
            emit "#rc	$trc"
            emit "#denied	$denied"
            emit "#endtop	$secs"
            bytes=$(awk -F '\t' -v i="$i" '$1 == "F" && $2 == i { s += $5 } END { printf "%.0f", s }' "$AGG")
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$t0" "$how" "$top" "$mt" "$bytes" "$ouid" >> "$STATE"
        done < "$OWN"
    done < "$WHO"
}

case $MODE in
usage)
    for p in "$@"; do usage_of "$p"; done
    ;;
scan)
    touch "$STATE"
    emit "#ages	$AGE1	$AGE2"
    [ -n "$OWNERS" ] && emit "#scan-as	owner"
    CAND="scan.cand.$$"
    LIST="scan.list.$$"
    : > "$CAND"
    # Each path as it really is (symbolic links resolved), to know a top
    # folder that is another of the paths.
    REALS=
    for p0 in "$@"; do
        if r=$(real "$p0") && [ -n "$r" ]; then REALS="$REALS$r$TAB$(norm "$p0")$NL"; fi
    done
    NESTED=
    SEEN=
    # Paths that listed nothing, or couldn't be listed: what the state says
    # of their folders is kept for a night they list again.
    KEEP=
    # Every top-level folder now, by its exact name (a glob, not a list that
    # a line break in a name could split); symbolic links and names with a
    # tab, line break or backslash are left out and counted.
    odd=0
    for p0 in "$@"; do
        p=$(norm "$p0")
        rp=$(real "$p")
        # The same path twice (as given, or through a link): scanned once.
        if [ -n "$rp" ]; then
            case $NL$SEEN in
                *"$NL$rp$TAB"*)
                    # Under another name (a link to it): said, so that
                    # figures stored under that name go. Under the same
                    # name (/home and /home/): nothing to say.
                    first=$(printf '%s' "$SEEN" | NOMAD_R="$rp" awk -F '\t' '$1 == ENVIRON["NOMAD_R"] { print $2; exit }')
                    [ "$p" = "$first" ] || NESTED="$NESTED#nested	$p		$first$NL"
                    continue ;;
            esac
            SEEN="$SEEN$rp$TAB$p$NL"
        fi
        # A path it can't list says so, rather than looking empty (which
        # would read as every folder in it gone).
        if ! ls -A "$p" >/dev/null 2>&1; then
            emit "#unreadable	$p"
            KEEP="$KEEP$p$NL"
            continue
        fi
        emit "#present	$p"
        listed=0
        for top in "$p"/* "$p"/.[!.]* "$p"/..?*; do
            [ -d "$top" ] && [ ! -L "$top" ] || continue
            name=${top#"$p"/}
            case $name in .zfs|.snapshot|.snapshots) continue ;; esac
            case $name in *"$NL"*|*"$TAB"*|*\\*) odd=$((odd + 1)); continue ;; esac
            # Another of this run's paths (it is no symbolic link: its real
            # path is its path's, and its name): that path scans it, once.
            if [ -n "$rp" ]; then
                case $NL$REALS in
                    *"$NL${rp%/}/$name$TAB"*)
                        other=$(printf '%s' "$REALS" | NOMAD_R="${rp%/}/$name" awk -F '\t' \
                            '$1 == ENVIRON["NOMAD_R"] { print $2; exit }')
                        NESTED="$NESTED#nested	$p	$(clean "$name")	$other$NL"
                        continue ;;
                esac
                # Holding another of the paths deeper down: that path's
                # folders are counted in this one too. Said, not hidden.
                inner=$(printf '%s' "$REALS" | NOMAD_R="${rp%/}/$name/" awk -F '\t' \
                    'index($1, ENVIRON["NOMAD_R"]) == 1 { print $2; exit }')
                [ -n "$inner" ] && NESTED="$NESTED#contains	$p	$(clean "$name")	$inner$NL"
            fi
            printf '%s\t%s\n' "$p" "$top" >> "$CAND"
            emit "P	$(clean "$name")"
            listed=$((listed + 1))
        done
        [ "$listed" -gt 0 ] || KEEP="$KEEP$p$NL"
    done
    emit "#odd-names	$odd"
    [ -n "$NESTED" ] && printf '%s' "$NESTED" >> "$TMP"
    # When each was last tried (0: never), oldest first; the state keeps only
    # the folders there now.
    awk -F '\t' 'FILENAME == ARGV[1] { last[$3] = $1; next }
                 { print (($2 in last) ? last[$2] : 0) "\t" $1 "\t" $2 }' "$STATE" "$CAND" \
        | sort -n > "$LIST"
    NOMAD_KEEP="$KEEP" awk -F '\t' '
        BEGIN { nk = split(ENVIRON["NOMAD_KEEP"], keep, "\n") }
        FILENAME == ARGV[1] { line[$3] = $0; next }
        FILENAME == ARGV[2] { if ($2 in line) { print line[$2]; delete line[$2] }; next }
        END { for (t in line) for (i = 1; i <= nk; i++) if (keep[i] != "" && index(t, keep[i] "/") == 1) { print line[t]; break } }' \
        "$STATE" "$CAND" > "$STATE.n" && mv "$STATE.n" "$STATE"
    left=0
    if [ -n "$OWNERS" ]; then
        owner_scan
    else
        while IFS="$TAB" read -r _last p top; do
            spent=$(( $(now) - started ))
            remaining=$((BUDGET - spent))
            if [ "$remaining" -lt 60 ]; then left=$((left + 1)); continue; fi
            t0=$(now)
            if [ -n "$GNUFIND" ]; then owner=$(stat -c %u "$top" 2>/dev/null); else owner=$(stat -f %u "$top" 2>/dev/null); fi
            emit "#top	$p	$(clean "${top#"$p"/}")	${owner:--}	$t0"
            : > "$ERRF"
            scan_top "$top" "$remaining" < /dev/null >> "$TMP"
            rc=$(tail -n 1 "$TMP")
            # What it couldn't read: an account without root misses folders.
            emit "#denied	$(grep -c 'ermission denied' "$ERRF")"
            emit "#endtop	$(( $(now) - t0 ))"
            # Each try goes to the back of the line, finished (find's 0, or 1 for
            # files that vanished or couldn't be read) or stopped at the budget,
            # so a folder too big for one night can't hold up the others.
            case $rc in "#rc	0"|"#rc	1") how="done" ;; *) how="partial" ;; esac
            printf '%s\t%s\t%s\n' "$t0" "$how" "$top" >> "$STATE"
        done < "$LIST"
    fi
    emit "#not-scanned	$left"
    ;;
esac

emit "#finished	$(now)"
chmod 640 "$TMP"
if [ -n "$GROUP" ]; then chgrp "$GROUP" "$TMP"; fi
mv "$TMP" "$OUT"
TMP=
