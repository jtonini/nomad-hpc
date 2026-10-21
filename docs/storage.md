# Storage use

Who holds each storage server's space, and on what (1.16): for each path a
server exports (`/home`, `/scratch`, a NAS's datasets),

- each owner's space now, and how it changed over the last 7, 30 or 90 days;
- their quota, where one is set (ZFS user, group and dataset quotas; XFS
  user, group and project limits);
- for each folder at the top of the path (`/home/USER`), the folders inside
  it (`/home/USER/FOLDER`): size, number of files, the newest change, and how
  much hasn't changed in 180 and in 365 days;
- when each owner last ran a job, on any cluster nomad collects.

It is meant to inform: what is filling a filesystem and how fast, what has
lain untouched for a year, and whose it is, including people who no longer
run anything. It changes nothing on the servers.

This is separate from the `storage` collector (`[collectors.storage]`, in
[Collectors](collectors.md)), which reads a server's pools and capacity, and
from `disk`, which reads `df` on each host.

## How it works

Seeing everyone's files takes root on the storage server (or sudo, or an
account given just those rights: see [Who runs it](#who-runs-it)), and nomad
itself never runs with them. So the part that reads runs there: a short POSIX
`sh` script, `nomad-storage.sh`, that only reads (`df`, `xfs_quota report`,
`zfs list`, `zfs get`, `zfs userspace` and `groupspace`, and for the scan
`find` and `stat`) and writes what it read to a folder that its owner and one
group can read (`/var/lib/nomad-storage` by default, mode 750, files 640).
The `storage_use` collector, on a site or on the hub, picks those files up
with an ordinary account in that group, over `ssh` or from the same machine,
and stores them.

The script runs on Linux (GNU find and stat) and on FreeBSD, as TrueNAS CORE
is (BSD find and stat); it needs nothing else installed. It writes:

- **`usage.txt`**, from the filesystem's own accounting: fast, so run it
  hourly. ZFS always keeps per-owner counts; XFS keeps them when user or group
  accounting is on (the `uquota`/`gquota` or `uqnoenforce`/`gqnoenforce` mount
  options; limits are not needed). Other filesystems (ext4, NFS) are listed
  with their size, and say that the scan counts them by folder instead. The
  counts are the whole dataset's or filesystem's: for a path inside one (not
  its mount point), the file says so, and so does `nomad storage use`.
- **`scan.txt`**, from walking the files: run it nightly. It works at the
  lowest priority (`nice 19`, and `ionice` idle on Linux), stays on one
  filesystem, and stops after its time budget (3 hours by default). The top
  folders tried longest ago go first, and each try, finished or stopped at
  the budget, goes to the back of the line: a large filesystem is covered
  over a few nights, and a folder too big for one night can't hold up the
  others (`nomad storage use` says how many never finish; give the scan more
  time with `-s`). A file with several hard links counts once in its top
  folder.

Sizes are the space files take on disk (as `du` says, after ZFS
compression), in both files. Snapshots aren't counted.

The script makes its folder itself, with a `.nomad-storage` file in it, and
refuses an existing folder it didn't make, so that a mistyped `--dir` can't
change the permissions of a pool or a home. The script, its folder and every
folder above them must be changeable only by root (or the account running
it), or it refuses to run, naming the folder and what to do: anyone else
could swap them for something else. A folder its group may write is refused
(even a group of one: accounts sharing its number don't always show), as is
one with an access list (ACL) that may let others write it; a umask of 002
leaves new folders group-writable, so `chmod g-w` the folders you make for
it.
It sets its own PATH, whatever the caller's. One run of each kind goes at a
time: a scan still going when the next is due is left to finish. Folders at
the top of a path whose names hold a tab, a line break or a backslash, and
symbolic links, are left out (and counted); files inside a top folder whose
paths hold a line break are counted together, in a folder of their own. A
path the scan can't list says so, and its folders keep their last figures.

## Setting it up

On a machine with nomad, print the script and how to run it:

```bash
nomad storage script > nomad-storage.sh
nomad storage cron /home /scratch --group nomadread --server fs1
```

`cron` prints the lines for root's crontab, the same for TrueNAS's Tasks >
Cron Jobs, and the `nomad.toml` part for the collector. `--group` is a group
on the storage server that the collecting account is in (the files are not
readable by anyone else); `--seconds` is the scan's budget; `--dir` where the
files go; `--run-as` who runs it.

### Who runs it

| `--run-as` | How | For |
|---|---|---|
| `root` (default) | root's crontab; on TrueNAS, Cron Jobs with *Run As User* `root` | a server where you are root |
| `sudo --user NAME` | NAME's crontab runs it with `sudo -n`; the sudoers lines it prints allow exactly those two command lines and nothing else | a server where you work through sudo |
| `sudo-readers --user NAME --dir D` | NAME's crontab runs it as NAME (`-S`), and it asks `sudo -n` only for `xfs_quota` and `zfs` (usage) and GNU `du` (the scan) | a Linux server where sudo already lets NAME run those, and nothing else as root |
| `user --user NAME` | systemd runs it as NAME with only the right to read every file and folder (`CAP_DAC_READ_SEARCH`), writing only to its folder | a Linux server where nothing should run as root |

In every case the script is owned by root and writable only by root
(`install -o root -m 755`), in folders only root can change: whoever can
change it can make it do anything its runner can. For sudo, the sudoers lines
name the folder, the group and the paths, and `cron` takes only paths of
letters, digits and `. _ / + @ , : = -`, which sudoers, cron and systemd all
read as written; with sudo's usual `env_reset`, the caller's environment
doesn't reach the script either.

With `sudo-readers`, the script and its folder belong to NAME (in NAME's home,
say) and nothing of nomad's runs as root but `xfs_quota`, `zfs` and `du`
themselves. The scan's figures are the same as with `find`, except that an
empty folder counts as a file and the owners of the folders inside each top
folder aren't read (the top folder's owner is).

Run as an account without root, the scan reads what that account may read,
and each top folder says how many files or folders it couldn't
(`nomad storage use`: "couldn't read"). On XFS, each owner's counts need
root (`xfs_quota`'s report): run `usage` as root or through sudo there, with a
folder of its own (root's runs won't write into a folder another account
owns), and the scan as the account; give the source `usage_dir` for the
usage folder. On ZFS, `zfs allow -u NAME
userused,groupused,userquota,groupquota DATASET` lets the account read the
counts. Every file says which account wrote it (`nomad storage status`).

### On a Linux file server

As root on the server:

```bash
install -o root -m 755 nomad-storage.sh /usr/local/sbin/nomad-storage.sh
sh /usr/local/sbin/nomad-storage.sh -g nomadread usage /home /scratch
head /var/lib/nomad-storage/usage.txt
crontab -e
```

and add the two lines `nomad storage cron` printed:

```
17 * * * * /bin/sh /usr/local/sbin/nomad-storage.sh -d /var/lib/nomad-storage -g nomadread usage /home /scratch
30 1 * * * /bin/sh /usr/local/sbin/nomad-storage.sh -d /var/lib/nomad-storage -g nomadread -s 10800 scan /home /scratch
```

cron's short PATH is fine: the script looks in `/usr/sbin` and `/sbin` for
`xfs_quota` and `zfs` itself.

### On TrueNAS

Changes on TrueNAS go through its web interface: the system disk is replaced
at upgrades, and anything put there by hand is lost. Put the script on a pool
(say `/mnt/POOL/nomad-storage/nomad-storage.sh`) and give it a folder there
that doesn't exist yet (`/mnt/POOL/nomad-storage/out`; the script makes it),
then in **Tasks > Cron Jobs > Add**, with **Run As User** `root`, add the
usage line (hourly) and the scan line (daily) that `nomad storage cron ...
--script /mnt/POOL/nomad-storage/nomad-storage.sh --dir
/mnt/POOL/nomad-storage/out` prints. The collecting account is a TrueNAS user
in the group given to `-g`, with SSH on; the folders above the output folder
must let it through (execute permission), and only root may change them
(`/mnt/POOL/nomad-storage` owned by root, not a share's dataset). On a dataset
with NFSv4 ACLs, its ACL mode must allow `chmod` (the script stops and says so
otherwise).

### The collector

In `nomad.toml` where the collector runs (a site, or the hub):

```toml
[collectors.storage_use]
enabled = true
# every_minutes = 60          # how often each server's files are read
# keep_days = 30              # every reading kept this long; one a day after
# timeout = 30                # seconds a server has to answer

[[collectors.storage_use.sources]]
server = "fs1"                # the name nomad shows
host = "fs1"                  # ssh to it; leave out when the files are on this machine
dir = "/var/lib/nomad-storage"
# ssh_user = "..."            # default: the account nomad collects as
# usage_dir = "..."           # usage.txt's own folder, when it isn't dir
# scan_dir = "..."            # (usage run as root, the scan as another account)
# copy_of = "fs0"             # it holds copies of fs0's files (see below)
# same_accounts = true        # it and this machine share their accounts (see Names)
```

The account needs a key for `ssh` with no password prompt (`BatchMode`), as
for the other collectors that reach servers. It reads the two files and
nothing else. A file is stored once, by when its run started; a run that
didn't finish leaves no new file (the script writes to a temporary file and
moves it into place at the end); a file that can't be stored is noted
(`nomad storage status`) without holding up the others. `nomad sync` copies
the tables to the hub. Read each server from one site: when two read the
same one, the hub shows the one that read it last, and `status` says so.

```bash
nomad collect -C storage_use
nomad storage status
```

### A server that holds copies

A server that holds replicas or backups of another's files (a second copy of
the homes, a transition copy kept until it is checked) is listed with
`copy_of`: it is read and shown like any other — whose files, how big, how
old, which helps when deciding what to keep — and marked as copies, never
added to anyone's totals.

## What it shows

```bash
nomad storage use                  # each path, its owners, largest first
nomad storage use --days 7         # the change over 7 days (30 by default; 90...)
nomad storage use --over 1T        # only owners holding more than 1 TiB
nomad storage use --over 500GB --list   # tab-separated lines to paste: folders, owner, used, last job, change
nomad storage use --binary         # sizes in KiB, MiB, GiB, TiB, as du -h and xfs_quota -h show them
nomad storage use --groups         # by group instead (--projects: XFS projects)
nomad storage use --server fs1 --top 30
nomad storage use --mask           # stand-ins for names and long paths, for sharing the output
nomad storage status               # what was read from each server, by whom, and when
```

```
fs1:/home  xfs  13.0 TB of 20.0 TB (65%)  read 2026-10-21 12:17
  scans: 780 of 800 top folders finished in the last 7 days, scanned 2026-10-12 to 2026-10-21
  owners as of 2026-10-21 12:17; the change is since 2026-09-21 12:17
  owner                used    30 days     quota   >365 days newest change   last job
  user 1            12.0 TB    +2.0 TB      none     40.2 GB    2026-10-20 2026-10-20
  user 2             1.0 TB        0 B      none    900.0 GB    2025-01-01       none
```

Every figure says when it is from: the reading, the reading the change is
from (the one nearest before 30 days ago, or `--days`), and the dates of the
scans behind the folders' figures. `last job` is
the latest job anyone with that username submitted on a cluster nomad
collects, so `none` can also mean someone who only uses the storage. A path
without per-owner counts lists its owners by what the scans found in their
top folders instead. ZFS datasets with a quota are listed under their path;
an owner with space in several datasets shows `several` for quota.

### Sizes

nomad shows sizes in powers of 1000 (KB, MB, GB, TB), as `df -H` does;
`--binary` shows them in powers of 1024 (KiB, MiB, GiB, TiB), as `du -h` and
`xfs_quota -h` do, so 1.7 TB is 1.5 TiB, which `xfs_quota -h` writes `1.5T`.
`--over` takes either way of writing a size: a bare letter (`500M`, `20G`,
`1T`) or `MiB`, `GiB`, `TiB` count in powers of 1024, as those tools write
them; `500MB`, `20GB`, `1TB` count in powers of 1000. `--list` gives one line
per owner (all of them, or those over `--over`; `--top` cuts it), its folders
as the scans found them (`/home/USER`, or wherever its top folders are; the
server and path until the first scan), and a note when the server holds
copies or the counts cover more than the path.

### Names

The usage file names owners as the storage server knows them, and a number
for an account it can't name, shown as `uid N` (`gid N`, `project N`): often
an account since deleted. When the server and the collecting machine share
their accounts (one directory), `same_accounts = true` on the source names
those numbers as the collecting machine knows them; a NAS with accounts of
its own should leave it out, or another person's name would be put on them. The scan records
each folder's owner as a number, named by the machine that collects it. When
the server and that machine share the same directory (LDAP, AD), the names
agree; a NAS with accounts of its own may number them differently, and its
scan's owners are then best read by folder name.

Folder names one level inside each top folder are stored (`/home/USER/FOLDER`),
never anything deeper, and stay where nomad's other data does. A top folder
that is gone from its path (a departed account's home, removed) goes from the
tables at the next scan. A top folder whose owner the collecting machine
can't name is counted as `uid N`, never under the person its folder is named
after. Use `--mask` for output that leaves your site.

### Quotas

None need to be set for any of this. Where they are, they are read and
shown: ZFS `userquota` and `groupquota` beside each owner, and dataset
`quota` and `refquota` under their path; XFS user, group and project soft and
hard limits beside each owner (`--groups`, `--projects`). A filesystem with
no accounting has no per-owner figures at all, quotas or not, and is covered
by the scan.

## Tables

| Table | Holds |
|---|---|
| `storage_reports` | each file read: server, usage or scan, when its run started and finished, who ran it, `copy_of` |
| `storage_paths` | each path's filesystem, size and use, what the counts cover, at each reading |
| `storage_owners` | each owner's space on each path (and ZFS dataset), quotas, at each reading |
| `storage_datasets` | ZFS filesystems under each path: use, quota, refquota |
| `storage_tops` | each scan of a top folder: owner, size, files, older than the two ages, whether it finished, what it couldn't read |
| `storage_folders` | the latest scan of each top folder's folders |
| `storage_reads` | each read, and what it found (working state, not copied to the hub) |

Every reading is kept for `keep_days` (30); after that, one whole reading a
day. A scan stopped at its budget doesn't replace a finished one of the same
folder.
