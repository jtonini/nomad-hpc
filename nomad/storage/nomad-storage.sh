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
#       first, and the next run goes on from there.
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
BUDGET=10800
AGES=180,365
NL='
'
TAB=$(printf '\t')

die() { printf 'nomad-storage: %s\n' "$*" >&2; exit 2; }

while getopts d:g:s:a:S opt; do
    case $opt in
        d) DIR=$OPTARG ;;
        g) GROUP=$OPTARG ;;
        S) SUDO=1 ;;
        s) BUDGET=$OPTARG ;;
        a) AGES=$OPTARG ;;
        *) die "usage: sh nomad-storage.sh [-d DIR] [-g GROUP] [-S] [-s SECONDS] [-a DAYS,DAYS] usage|scan PATH..." ;;
    esac
done
shift $((OPTIND - 1))
MODE=${1:-}
[ $# -gt 0 ] && shift
case $MODE in usage|scan) ;; *) die "say usage or scan, then the paths" ;; esac
[ $# -gt 0 ] || die "no path given"
case $DIR in /*) ;; *) die "-d: an absolute folder" ;; esac
case $BUDGET in ''|*[!0-9]*) die "-s: a number of seconds" ;; esac
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
STATE=scan.state
cleanup() {
    [ -n "$TMP" ] && rm -f "$TMP" "$TMP.ds"
    [ -n "$ERRF" ] && rm -f "$ERRF"
    [ -n "$CAND" ] && rm -f "$CAND" "$LIST" "$STATE.n"
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

if find / -maxdepth 0 -printf '' >/dev/null 2>&1; then GNUFIND=1; else GNUFIND=; fi
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

case $MODE in
usage)
    for p in "$@"; do usage_of "$p"; done
    ;;
scan)
    touch "$STATE"
    emit "#ages	$AGE1	$AGE2"
    CAND="scan.cand.$$"
    LIST="scan.list.$$"
    : > "$CAND"
    # Every top-level folder now, by its exact name (a glob, not a list that
    # a line break in a name could split); symbolic links and names with a
    # tab, line break or backslash are left out and counted.
    odd=0
    for p0 in "$@"; do
        p=$(norm "$p0")
        # A path it can't list says so, rather than looking empty (which
        # would read as every folder in it gone).
        if ! ls -A "$p" >/dev/null 2>&1; then
            emit "#unreadable	$p"
            continue
        fi
        emit "#present	$p"
        for top in "$p"/* "$p"/.[!.]* "$p"/..?*; do
            [ -d "$top" ] && [ ! -L "$top" ] || continue
            name=${top#"$p"/}
            case $name in .zfs|.snapshot|.snapshots) continue ;; esac
            case $name in *"$NL"*|*"$TAB"*|*\\*) odd=$((odd + 1)); continue ;; esac
            printf '%s\t%s\n' "$p" "$top" >> "$CAND"
            emit "P	$(clean "$name")"
        done
    done
    emit "#odd-names	$odd"
    # When each was last tried (0: never), oldest first; the state keeps only
    # the folders there now.
    awk -F '\t' 'FILENAME == ARGV[1] { last[$3] = $1; next }
                 { print (($2 in last) ? last[$2] : 0) "\t" $1 "\t" $2 }' "$STATE" "$CAND" \
        | sort -n > "$LIST"
    awk -F '\t' 'FILENAME == ARGV[1] { line[$3] = $0; next } ($2 in line) { print line[$2] }' \
        "$STATE" "$CAND" > "$STATE.n" && mv "$STATE.n" "$STATE"
    left=0
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
    emit "#not-scanned	$left"
    ;;
esac

emit "#finished	$(now)"
chmod 640 "$TMP"
if [ -n "$GROUP" ]; then chgrp "$GROUP" "$TMP"; fi
mv "$TMP" "$OUT"
TMP=
