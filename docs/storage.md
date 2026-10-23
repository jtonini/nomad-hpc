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
  folder. A folder at the top of a path that is itself another of the paths
  given to the same run (`/mnt/pool/home` when the run has `/mnt/pool` and
  `/mnt/pool/home`, or a symbolic link to it) is left to that path, so its
  files are counted once, and a path given twice is scanned once; `nomad
  storage status` lists both. A top folder that holds another of the paths
  deeper down (`/mnt/pool/labs` when the run also has
  `/mnt/pool/labs/shared`) is still counted whole, so that path's files are
  counted in both: `nomad storage status` says so, and the fix is to leave
  one of the two out of the run.

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
| `owners --user NAME --dir D` | NAME's crontab runs it as NAME, and the scan measures each top folder as its owner (`-O`: `sudo -n -u OWNER find`) | a Linux server where sudo lets NAME act as the other accounts but not as root (see [Measuring as each owner](#measuring-as-each-owner)) |
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

### Measuring as each owner

Some servers give an account the right to act as the other accounts through
sudo (to install software for them, say), but not as root. There, `-O` (`nomad
storage cron --run-as owners`) measures each top folder as the account that
owns it, which may read all of it even when no one else may.

- **What it runs:** for each owner, one `sudo -n -u '#UID' find` (by number,
  so it is exactly the folder's owner), for all of that owner's top folders
  on every path, from `/`, read only, at the lowest priority (`nice 19`,
  `ionice` idle) and within the scan's budget. It prints each folder's depth,
  owner and path and each file's size, time, links and inode, nothing else.
  Paths are passed as arguments, never through a shell, and an owner is acted
  as only when the system gives it a name made of letters, digits and `. _ -`.
  A folder of the account running the script is read without sudo.
- **The sudo rule it relies on:** either one sudo already has, letting the
  account run commands as people's accounts (`sudo -l` as the account shows a
  rule such as `(%GROUP) ALL`), or, where there is none, a rule for
  `nomad-storage-find` alone. Never give it `find` itself: find can run any
  program (`-exec`), delete and write files, so a rule for find is a rule for
  anything, as any account it names, slurm's and munge's included.
  `nomad-storage-find` is a few lines of `sh` that take absolute folder paths
  and run find over them with the fixed, printing-only expression above. As
  root (sudo 1.8.28 or later):

  ```
  nomad storage script --find > nomad-storage-find
  install -o root -g root -m 755 nomad-storage-find /usr/local/sbin/nomad-storage-find
  visudo -f /etc/sudoers.d/nomad-storage
      NAME ALL=(ALL,!root) NOPASSWD: /usr/local/sbin/nomad-storage-find
  ```

  and give the script `-F /usr/local/sbin/nomad-storage-find` (`nomad storage
  cron --run-as owners --find-wrapper /usr/local/sbin/nomad-storage-find`).
  It refuses a wrapper that isn't nomad-storage-find, or that anyone but
  root could change or swap: give its real path, with no link in it (sudoers
  names that path), and the file and every folder from `/` down to it must
  be root's and writable by root only.
  `(%GROUP)` in place of `(ALL,!root)` narrows it to one group's accounts.
  Where sudoers has `Defaults requiretty` (older RHEL), add
  `Defaults:NAME !requiretty`: cron has no terminal, and sudo would refuse
  every owner.
- **How often:** each top folder weekly, on a night of its own (spread over
  the seven by its path), and nightly when it is large (10 GB or more, `-L GB`,
  `--large`) or changed since it was last measured (a plain `stat` of it: an
  entry made, removed or renamed directly in it, or a new owner or mode; not
  its ctime, which backups that keep access times change every night). A folder whose last
  measurement didn't finish is measured again the next night, and one missed
  for more than a week is caught up. The others keep their last measurement,
  shown with when it was taken (`measured` in `nomad storage use`). Each
  night's file holds only that night's measurements, so the collector must
  read it before the next night's run (it reads every few minutes): a night
  it misses leaves those folders at their earlier figures until they are
  measured again.
- **What isn't measured:** top folders of root and of system accounts
  (numbered below 1000, `-U UID`, `--min-uid`), for which sudo isn't even
  asked; of owners sudo refuses (an account the rule leaves out), asked again
  only after a week, or sooner if the folder changes hands, since sudo logs
  every refusal; and of owners with no name. Their last figures stand, if
  there are any, and `nomad storage status` says how many owners and top
  folders weren't measured, and why. A folder inside
  a top folder that belongs to someone else is read only as far as the owner
  of the top folder may read it (`couldn't read`, counted from find's own
  output, in any locale). Such a measurement is short of what is there:
  `nomad storage status` and `nomad storage use` flag it. It is still at
  least what is there, so it stands when it is larger than the folder's last
  whole measurement (the folder grew); when it is smaller (an owner may make
  a folder unreadable to hide it), the last whole one stands, for four weeks
  at most (a lab folder holding a member's private folder is never read
  whole, and a real cleanup has to show), and never once the top folder has
  a new owner (the old owner's figure says nothing of the new one's). A
  folder with no earlier figure (new, or renamed: where people make their
  own top folders, as on a shared scratch) has nothing to hold, so its first
  short figure stands, flagged. When the
  scan's time runs out, the folders the owner's find had finished count as
  measured, the one it was in is short, and the rest wait for the next
  night.
- **Each owner's total** comes from the scan, as on a filesystem without
  per-owner accounting (`usage`, run as an ordinary account, reads only `df`).
  The collector reads the files on the server itself (a source without
  `host`) or over ssh, as for the other ways of running it.

It needs GNU find (Linux), and can't be combined with `-S`. The alternative is
one line in sudoers that lets the account run the script itself as root
(`--run-as sudo`): root owns the script, and the account can't change it.
That reads everything, every night, within the budget, in one pass.

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
# copies = {"/backup/home" = "fs0:/home"}   # or only some of its paths do
# same_accounts = true        # it and this machine share their accounts (see Names)
# account_floor = 1000        # ... from this number up (see Names); also above, for all
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

When only some of its paths hold copies (a backup of the homes beside a
scratch area of its own), name them with `copies`, each with the server and
path it copies (or just the server, when the path is the same there):

```toml
copies = {"/backup/home" = "fs0:/home"}
```

Those paths are shown as copies of `fs0:/home` and left out of totals; the
server's other paths count as usual.

## What it shows

```bash
nomad storage use                  # each path, its owners, largest first
nomad storage use --days 7         # the change over 7 days (30 by default; 90...)
nomad storage use --over 1T        # only owners holding more than 1 TiB
nomad storage use --over 500GB --list   # tab-separated lines to paste: folders, owner, used, last job, change
nomad storage use --binary         # sizes in KiB, MiB, GiB, TiB, as du -h and xfs_quota -h show them
nomad storage use --groups         # by group instead (--projects: XFS projects)
nomad storage use --server fs1 --top 30
nomad storage use --path /home     # one path, on every server that has it
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
top folders instead, each with when it was measured (the oldest scan behind
its figures). ZFS datasets with a quota are listed under their path;
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
an account since deleted. The scan records each folder's owner as a number.
Which numbers the collecting machine names, as it knows them:

- **The files of the machine itself** (a source without `host`, written by
  this machine): all of them. A source without `host` whose files another
  machine wrote (a folder shared over NFS, say: the file names the machine
  that ran the script) is named as a server sharing its accounts, below.
- **A server sharing its accounts** (one directory, LDAP or AD:
  `same_accounts = true` on the source): those from `account_floor` up (1000
  by default, where most Linux systems start their people's accounts; set it
  for every source under `[collectors.storage_use]`, or on one). Below it are
  each machine's own system accounts, which may be other accounts on the
  server than here, so they stay numbers.
- **Any other server** (a NAS with accounts of its own): none, or another
  person's name could be put on them. Its owners are shown as `uid N`, and are
  best read by folder name; `nomad storage status` says so, for a server that
  in fact shares this machine's accounts and only lacks `same_accounts =
  true`. (Before 1.17.1 a scan's owners were named whatever the server, so
  after the upgrade such a server's scan owners turn to numbers until it is
  set.)

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
| `storage_reports` | each file read: server, usage or scan, when its run started and finished, who ran it, `copy_of`, `copies` (per path), how numbered owners were named (`naming`: local, same or none); for a scan, the folders left out as other paths (`nested`), those holding another path (`contains`), how many were measured short (`short_tops`), and with `-O` (`scan_as` owner) those kept from earlier nights (`carried`) and those not measured |
| `storage_paths` | each path's filesystem, size and use, what the counts cover, at each reading |
| `storage_owners` | each owner's space on each path (and ZFS dataset), quotas, at each reading |
| `storage_datasets` | ZFS filesystems under each path: use, quota, refquota |
| `storage_tops` | each scan of a top folder: owner, size, files, older than the two ages, whether it finished (and if not, why: `why_short`, time or unreadable), what it couldn't read, when |
| `storage_folders` | the latest scan of each top folder's folders |
| `storage_reads` | each read, and what it found (working state, not copied to the hub) |

Every reading is kept for `keep_days` (30); after that, one whole reading a
day, and one scan of each top folder a week (the week's latest finished one,
else its latest). A short scan (stopped at its budget, or as an owner who
can't read everything) replaces a finished one of the same folder only when
it found more.
