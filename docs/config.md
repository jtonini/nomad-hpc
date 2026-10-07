# Configuration

NØMAÐ uses TOML configuration files.

## Configuration Locations

| Install Type | Path |
|--------------|------|
| User | `~/.config/nomad/nomad.toml` |
| System | `/etc/nomad/nomad.toml` |

### When the file can't be read

A file with a mistake in it is not read at all: nomad then runs with **none**
of its settings (database, collectors, roles, labs), and the Console shows no
PIs or labs. It is not replaced by the next file either (`/etc/nomad/nomad.toml`
behind a broken `~/.config` one): only nomad's packaged defaults apply. So
nomad says so instead of carrying on quietly:

- every command prints, on stderr, which file and which line (when nobody is
  at a terminal, as under cron, once an hour for the same problem);
- `nomad syscheck` marks it ✗, and `nomad console roles` says the Console has
  no labs until it is fixed;
- `nomad lab` refuses to edit it.

After editing the file by hand, check it:

```
$ nomad config check
✗ /home/hpcadmin/.config/nomad/nomad.toml can't be read
  line 211: `leads` is set twice in [console.labs] (first on line 209); keep one of them
    209 | leads = { jdoe = ["pi1$"] }
  > 211 | leads = {}
```

A key set twice in the same table (as above), or a table declared twice, is
the usual cause. The exit status is 1 while the file can't be read, 0 once it
does; the messages name keys and tables, never values from the file.

## Example Configuration
```toml
# nomad.toml

[general]
cluster_name = "spydur"
data_dir = "/var/lib/nomad"
log_level = "INFO"

[database]
path = "/var/lib/nomad/nomad.db"

[collectors]
interval = 60  # seconds, for `nomad collect` without --once

# Each collector is turned on or off in its own table (see Collectors).
[collectors.nfs]
enabled = false

[collectors.disk]
filesystems = ["/", "/home", "/scratch"]
quota_enabled = true

[collectors.slurm]
partitions = ["compute", "gpu", "highmem"]

[dashboard]
host = "127.0.0.1"
port = 8050

[alerts]
enabled = true

[mail]
host = "smtp.example.edu"
from = "hpc@example.edu"

[support]
email = "hpc-support@example.edu"
institution = "Example University"

[alerts.email]
enabled = true
recipients = ["admin@example.edu"]

[alerts.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/..."

[alerts.thresholds]
disk_warning = 85
disk_critical = 95
gpu_temp_warning = 80

[ml]
enabled = true
similarity_threshold = 0.7
```

## Console access

Who may see what in the Console is set in `nomad.toml` on the machine that
runs it:

```toml
[console.roles]
admin = ["jtonini"]            # everything, settings included
operator = []                  # everything, no settings

[console.labs]
group_pattern = "{netid}$"     # the group a faculty member leads, by its name
leads = { NETID = ["chemlab$"] }   # exceptions and extra groups

[console.labs.resources."NETID$"]
name = "Smith Lab"             # how the lab is shown (else its group)
workstations = ["adam", "eve"] # the lab's own machines
storage = ["sarahvaughan"]     # its storage: a server, or server:/export

[console.storage."10.0.0.28"]  # a storage server, as the mounts name it
name = "sarahvaughan"
note = "community $HOME, all users"
```

- **Viewer:** anyone the file does not name who signs in. They see their own
  work, and the aggregates (queues, capacity, storage) everyone sees.
- **PI:** a viewer who leads a lab also sees its members, one by one. They
  lead the group `group_pattern` names for them, when that group exists in the
  groups collector's data, and any group listed for them in `leads`
  (`nomad lab lead LAB NETID` adds one, below). Members are the people the
  group lists now who still have an account; someone taken out of the group,
  or whose account was deleted while a group file still names them, is not
  one (see the groups collector).
  `group_pattern` is empty by default: a site that has not said how its groups
  work gets no lab view rather than a wrong one.
- **A lab's machines:** a PI also sees their labs' workstations and storage.
  On those machines, people outside the lab appear as "another user".
  A workstation belongs to a lab when the collector that monitors it tags it
  with the lab's group: `department = "NETID$"` in its
  `[[collectors.workstation.workstations]]` entry, on the machine that
  collects it. The tag reaches the hub with the data, so a workstation is
  assigned once, where it is added. A machine moved to another lab follows its
  latest tag. Workstations can also be listed under
  `[console.labs.resources."group"]` (one not collected, say), and storage is
  listed there: a storage server's name, or one export (`server:/export`) as
  the workstations mount it. Nothing is guessed from names.
- **Storage servers:** `[console.storage."SERVER"]` gives a server a `name`
  and a `note`, shown wherever its exports are. When the storage collector
  reads that server (under its address, or under that name, as an ssh
  alias), its state, space and pools' health go on the server's line. SERVER is the server as the
  mounts name it, the part before the colon in `server:/export` (often an
  address). A server that everyone shares should say so in its note: its
  used space is everyone's, and would otherwise read as the lab's own.
- **Operator:** sees everything, changes no settings.
- **Admin:** everything, settings included.

NetIDs are compared without regard to case. For anyone named here the file
wins over the Console's own records (`users.json`), which keep only what is
private or automatic: the break-glass password, and the record made at
someone's first login. A setting that is wrong is reported and left out,
never guessed at.

### Adding machines and labs: `nomad lab`

`nomad lab` edits these settings in `nomad.toml` for you: it backs up the
file first, keeps its comments, and writes nothing that doesn't read back as
intended. Each command shows the change; `--apply` writes it.

```
nomad lab show [LAB]
nomad lab add-machine LAB HOST          a workstation collected here, tagged with the lab
nomad lab add-nas LAB HOST [--name NAME] [--note "..."] [--type zfs|nfs] [--path P]
                                        a NAS collected here (its pools), listed as the lab's
nomad lab add-storage LAB SERVER[:/export] [--name NAME] [--note "..."]
                                        storage the lab's machines mount, listed as the lab's
nomad lab name LAB "NAME"               how the lab is shown, e.g. "Smith Lab"
nomad lab lead LAB NETID [--remove]     NETID leads LAB too (or no longer)
nomad lab remove LAB HOST
```

For storage that is everyone's, LAB is `shared`: `nomad lab add-nas shared
HOST --note "community $HOME"` collects it and names it, listed for no lab.
It stays collected when a lab that also lists it removes it.

LAB is the PI's NetID when `group_pattern` is set (`jdoe` is `jdoe$`
with `"{netid}$"`), or the lab's group itself.

- **add-machine** goes on the host that collects the workstation (a lab's
  own head node, or the hub for a lab without one). It turns the workstation
  collector on there if needed, and says whether the host answers over SSH
  with a key.
- **add-nas** is for a NAS the storage collector reads over SSH: pool health,
  and the space users have (each pool's root dataset, so RAIDZ parity isn't
  counted; a boot pool isn't storage). A host listed as a workstation moves
  to storage. It says whether cron on this host runs the storage collector.
- **add-storage** goes on the Console's machine: an export the lab's machines
  mount, or a server, with an optional name and note for the server.
- **lead** goes on the Console's machine: someone besides the PI who leads
  the lab (a co-PI, a lab manager, a test account) sees its machines and,
  one by one, its members. LAB may also be the lab's name as `nomad lab
  show` prints it, and must be a lab nomad knows (listed machines or storage,
  tagged workstations, or members in the groups data) unless `--new-group`
  is given: a mistyped NetID could be another PI's lab. It writes
  `[console.labs.leads]`; a `leads = {}` line in `[console.labs]`, as in
  `nomad.toml.example`, becomes that table. Editing `leads` by hand risks
  setting it twice, which makes the whole file unreadable. The PI
  `group_pattern` names needs no entry.
- **remove** takes a host out of a lab: no longer collected here, no longer
  listed.

A workstation's lab travels with its data, so it is added where it is
collected; storage listings and server names are read on the Console's
machine. On a hub that also collects a lab, one command does all of it.

`nomad console roles` shows what the file grants, and anything wrong in it;
`nomad console roles NETID` shows what that person would see, with each lab's
size, and whether each of their machines is online (and when it last was) or
whether each storage export is mounted and responding, by server (with its
name and note), with its space when the lab machines report it: percent used, used of total and free, in decimal units
(those of `df -H`), and for exports of one pool "free space shared with ..."
and a line with their space together (see docs/collectors.md, Workstation
mounts), and for a NAS the storage collector reads, its state, space and
pools' health. A machine or NAS whose latest record is over an hour old
shows as "no report since ..."; a workstation with no record in two days no
longer counts as its lab's. A machine known only by
its tag is marked `[tagged]` (`--db` for the database holding group membership, such as the hub's
combined database; `--mask` for counts only).

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NOMAD_CONFIG` | Config file path |
| `NOMAD_DB` | Database path |
| `NOMAD_LOG_LEVEL` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `NOMAD_CONSOLE_HOST` | The machine serving the Console, for `nomad console` |
| `NOMAD_LOGIN_HOST` | This cluster's name as people's computers reach it, for `nomad console` |

## Collectors

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed collector documentation.
