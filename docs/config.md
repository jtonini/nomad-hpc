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
viewer = []                    # named: their own work, whatever else says

[console.labs]
group_pattern = "{netid}$"     # the group a faculty member leads, by its name
leads = { NETID = ["chemlab$"] }   # exceptions and extra groups

[console.labs.resources."NETID$"]
name = "Smith Lab"             # how the lab is shown (else its group)
workstations = ["adam", "eve"] # the lab's own machines
storage = ["sarahvaughan"]     # its storage: a server, or server:/export

[console.labs.assistants]
"NETID$" = ["eve"]             # research assistants: see the lab as its PI does

[console.labs.attention]       # what My lab's Needs attention lists (1.17.6); the defaults:
# days = 7                     # the jobs, and the growth of data, looked at
# waiting_hours = 24           # a job waiting longer for something that won't change by itself,
# waiting_max_days = 60        # and no longer (an old record, not a job)
# failing_jobs = 3             # failed jobs, at least half of a member's that ran to an end
# storage_pct = 90             # a lab storage server this full
# holders = 3                  # the lab's biggest holders of data named (0: none),
# holder_gb = 100              # each holding this much or more,
# growth_gb = 100              # and anyone whose data grew this much in `days` (0: none)
# quota_pct = 90               # a member this close to a quota
# over_cpu_hours = 100         # core-hours held by measured jobs,
# over_cpu_pct = 25            # used under this share of them
# over_memory_pct = 20         # memory used under this share of what jobs asked for,
# over_memory_jobs = 3         # over this many measured jobs
# workstation_hours = 2        # a lab workstation silent this long

[console.storage."10.0.0.28"]  # a storage server, as the mounts name it
name = "sarahvaughan"
note = "community $HOME, all users"
```

`nomad console role NETID admin|operator|viewer|none` changes one person's
role here (`--apply` writes it): only their name in the `admin`, `operator`
and `viewer` lists changes, and each list's line and comment stay. `viewer`
names them a viewer whatever else would make them more (a directory admin
group, an older role in the Console's own records); `none` takes them off
every list, so the Console's own record decides. Roles written inline
(`roles = {...}`), a list spread over several lines, or a multi-line string
are left to be changed by hand. It refuses to write when the file changed
since it was read, keeps the file's line endings, and keeps the newest 20 of
its backups (`nomad.toml.bak-role-*`; ones named by hand stay). Give it the
file the Console reads. Saving writes in the file's folder (the backup, then
the new file swapped in), so run it as an account that can write that folder,
or as root (`sudo -u nomad nomad -c /etc/nomad/nomad.toml console role jdoe
operator --apply` where the nomad account or its group may write /etc/nomad;
else `sudo nomad -c ...`).

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
  A group that is a course this term (`[console.courses]`: listed, as
  `nomad course add` on an instructor's own group does, or named like a
  course) is a class, not a lab: that term it is not the instructor's lab,
  unless storage or machines are listed for it under
  `[console.labs.resources]` or a `leads` entry names it (`nomad lab lead
  LAB NETID` writes one, for the instructor too: its PI and research
  assistants then see its members, the class's students, one by one); a
  workstation only tagged with the group doesn't count. `nomad console roles
  NETID` and `nomad facts lab NETID` say when someone's own group is a class
  this term. `nomad course add` says so when it makes a
  lab a class. It is a lab again the term it isn't a course. The condo
  reports' members are the owner's group either way.
- **Research assistant:** a viewer listed for a lab under
  `[console.labs.assistants]` (`nomad lab assistant LAB NETID`, below, or the
  PI from the lab's Members page in the Console) sees it as its PI does: its
  members one by one, its machines and storage -- but for whoever leads it:
  the PI's own work stays theirs. Only while they are one of
  the lab's members: someone who leaves the group stops being its assistant
  with nothing to edit, and `nomad console roles` lists the entries that no
  longer apply. Only a lab has them: a group named in `[console.labs]`
  (machines, storage, a name, a lead) or the one `group_pattern` names for
  one of its own members, not any group with members; a lab that is a
  class this term (above) has none that term. Names that aren't NetIDs are
  reported and left out.
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
- **Needs attention:** the lab's PI (and its research assistants, and
  staff) see at the top of each lab what needs them: members' jobs stuck
  waiting, out of memory, out of time or mostly failing, the lab's storage
  filling, who holds the most data and where, someone near a quota, jobs
  holding far more than they use, a workstation gone quiet
  ([details](facts.md#lab)). `[console.labs.attention]` sets each limit;
  `nomad config check` reports a value it can't use (its default applies).
  `nomad facts lab NETID --each` prints the same list.
- **Storage servers:** `[console.storage."SERVER"]` gives a server a `name`
  and a `note`, shown wherever its exports are (and beside a member's data in
  Needs attention: a note saying what it is, "spydur's /home and /scratch",
  tells a PI where that is). When the storage collector
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
nomad lab assistant LAB NETID [--remove]
                                        NETID is a research assistant of LAB (or no longer)
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
- **assistant** goes on the Console's machine: a research assistant sees
  the lab as its PI does while one of its members (above). LAB as for
  `lead`, and a lab by nomad's rules (above), not just any group with
  members; taking a name off works for any. It says how many members the lab has, and when NETID isn't one of
  them in the groups data now (the entry applies once they are), and writes
  `[console.labs.assistants]`, which goes when its last name does. Like
  `leads`, the table is written whole: comments inside it aren't kept, and
  an entry nomad can't read is left for a person to fix first.
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

### Courses: `nomad course`

A course is a Unix group (`demo101`, say), a software environment, or both.
Its instructors see it in `nomad facts course` (and the Console): the class
this term, each student as a row, when the class works. Nothing is a course
until `[console.courses]` is there: a site that hasn't said how its classes
work gets no course view rather than a wrong one. `nomad course setup`
asks, showing what it finds here for each answer.

```toml
[console.courses]
sources = ["groups", "environments"]     # how classes get access (below)
pattern = "^[a-z]{2,6}[0-9]{3}[a-z]?$"   # which groups and environments are courses, by name
faculty_group = "faculty"                # whose members can teach one
term_start = "2026-08-24"                # this term's first day
min_students = 0                         # fewer students this term: counts only
names = false                            # instructors see students by name
not_teaching = ["jdoe"]                  # in the faculty group, teaches no course (staff)

[console.courses.groups."demo101"]
name = "Econometrics"
instructors = ["jdoe"]                   # teaches it every term
exclude = ["asmith"]                     # in the group and faculty, never teaches it
environments = ["datakit"]              # whoever used it this term is in the class

[console.courses.groups."demo101".terms."2026-08-24"]
instructors = ["bsmith"]                 # teaches it this term
exclude = ["cjones"]                     # doesn't teach it this term

[console.courses.groups."lab201"]
course = false                           # named like a course, but isn't one
```

- **How classes get access** differs from one institution to the next;
  `sources` says which of these happen here (default `["groups"]`):
  - `groups`: a Unix group per course, named for it. A group whose name
    matches `pattern` is a course; its members are the course's people.
  - `environments`: a software environment per course, a conda environment
    or a Jupyter kernel named for it. The interactive servers record which
    environment each session runs from (nomad 1.12.5; `environment_patterns`
    under the interactive collector, [collectors](collectors.md)), and
    whether it is the person's own (they own it, or keep it in their home):
    anyone can name one like a course, so a session from one's own counts
    only for a faculty member (an instructor who built their course's). An
    environment named like a course group
    (`pattern`) counts for that course: whoever used it this term, on two
    days or more, is one of its members, whatever groups they are in. Shared
    software is named like courses too (`py311`, `cuda121`): an environment
    with no group of its name is a course only when listed (`nomad course
    env NAME NAME`).
  - `own_groups`: an instructor's own group (`jdoe$`, by `[console.labs]
    group_pattern`) holds their class. Such a group is often their lab too,
    so none becomes a course by itself: `nomad course discover` proposes the
    ones with people active this term, and one becomes a course when someone
    is added to it. A course this term, it is not their lab that term (see
    the PI under Console access): add one that mostly holds a class.

  Whatever the sources, a group listed under `groups` is a course (unless
  `course = false`), and a course's `environments` add whoever used them this
  term to its students (`nomad course env GROUP ENV`): for a class whose
  environment isn't named like its course. They add no one to choose its
  instructors among: an environment two courses share says who is in a
  class, not who teaches it (its users are students of both). An
  environment listed under a course counts for that course, not as one of
  its own. Environments are listed for good; a group listed only by a term's
  decisions (someone added to an instructor's own group for a term) is a
  course that term only.
- A course's **instructors this term**, in this order:
  1. anyone listed (`nomad course add`), this term or every term. When
     anyone is, they alone teach it that term: nothing below adds anyone,
     and nobody is asked about it in the Console. A group reused from an
     earlier term keeps that term's instructor among its members, so adding
     this term's takes the earlier one out; list a co-instructor or a TA too;
  2. otherwise, of its members in `faculty_group` (its group's, and whoever used the
     environment named for it), those who said in the Console that they
     teach it this term (`nomad course answers`);
  3. when none has, its one faculty member, when it has one;
  4. when it has several, those who joined its group this term;
  5. when none did, those who used its environment this term;
  6. when none did, those active this term where the course's students work
     (the servers and clusters most of its active students used: research
     on another cluster says nothing about a course).

  Groups are reused term after term and nobody takes last term's instructor
  out; when nothing above settles it, nomad can't tell who teaches it, and
  nobody does until someone says so or is added. Left out: anyone excluded
  from the group (`nomad course exclude`), this term or every term; anyone
  who said they don't teach it this term; anyone in `not_teaching` (staff in
  the faculty group, old accounts, people who retired; `nomad course
  not-teaching`) unless added to the course by name. nomad.toml wins over
  what people said, and both over nomad's guess. A course with no instructor
  is shown to no one; `nomad course list` names those last.
- **Decisions hold for a term.** Who teaches a course changes from term to
  term, so `nomad course add` and `exclude` write under the term's own table
  and stop applying when `term_start` moves on (they stay in nomad.toml as a
  record); `--every-term` writes them for good. So do the answers people give
  in the Console: each is for one term.
- **`term_start` must move on.** When it is set, a new term begins only when
  it is changed (`nomad course setup --term-start YYYY-MM-DD`); `nomad course
  list` says when the calendar's term has moved past it. Left unset, it is the
  Monday on or after 15 August, 1 January or 1 June (a US academic calendar);
  set it to the first day of classes, or the weeks before would count in each
  student's range as weeks of nothing.
- Activity is a person's, not a course's: a faculty member active this term
  where a course's students work counts in every group of several they are
  in, and one who teaches without using the server is left out. `nomad
  course list` shows why each instructor is there; check it once a term.
  `course add` takes no one off. Join dates say more than activity: an
  instructor added to a reused group shows as joined that term (a
  membership nomad saw before 1.11, on any site, has no date); someone added
  only for access counts as joined too: exclude them.
- **What people say** is kept beside the hub's combined database
  (`course_answers.db`, readable by its owner only), not in nomad.toml.
  Only the course's faculty members can answer (in its group, or a user of
  the environment named for it; not those excluded, staff, or those already
  added by name); `nomad course answer GROUP NETID
  --yes|--no|--clear` records an answer someone gave you another way.
- Its **students** are its other people; of those, **this term's** joined its
  group since `term_start` or used an interactive server or a cluster since
  then (groups nobody prunes keep earlier terms' students). For a course
  whose class is in its environment rather than its group -- more of the
  environment's users are outside the group than the group has students who
  joined or were active this term; or, early in a term, a faculty member
  outside the group uses it, none of the group's does, and fewer than half
  the group's active students do -- the environment says it: this term's
  students joined its group this term or used its environment, an earlier
  student's work this term in another class doesn't count, and its group's
  faculty members count only if they joined it this term or used the
  environment (left out otherwise: not last term's instructor shown this
  term's class). One outsider in a course's kernel changes nothing. nomad
  records when a membership began from 1.11 on; before that, a member who
  hasn't been active this term can't be told from an earlier term's, and is
  left out (and counted as such).
- A group named like a course that isn't one (a project's or a lab's) is
  marked `course = false` (`nomad course ignore GROUP`): otherwise every
  faculty member in it would see its other members as a class. `nomad course
  list` shows every group taken for a course.
- `min_students` defaults to 0: a small institution's course may have three
  students. With so few, even figures for the class together are close to
  each student's, and unnamed rows are not anonymous to someone who knows
  the class. `names` (default false) decides whether instructors see each
  student by name: both are for the institution to decide (FERPA).

A faculty member who used the environment named for a course on two days or
more this term is among those it chooses its instructors from when they are
in its group, or when its class is in the environment (above); with several,
those who used it count as teaching it. A faculty member outside a course's
group who used its environment counts only when people besides faculty
outside the group used it too (a colleague who borrowed a group course's
kernel is not one).
`nomad course list` shows why; exclude whoever doesn't teach it. Its students' figures are all their work
this term, as for a group's students, not only in its environment.

**`nomad course discover`** (read-only) lists what may be a course that
nomad.toml doesn't say is one yet: each environment people had a session in
this term (how many, the faculty members among them, the course it counts
for, the group most of its users are in), and groups with a faculty member
and at least three people active this term besides faculty (whose own group
each is, and the environment most of them used). Then what to do, as
commands that each show their change first: environments named like a
course group (`setup --sources ...,environments`), one named like a course
with no group (`course env NAME NAME`), an environment four in five of whose
users are in a course's group (`course env`), an instructor's own group
whose people are active (`course add`, for this term, when `own_groups` is a
source). Nothing for an environment several faculty members use: that is
shared software, not a class's.
`--mask` numbers groups, environments and NetIDs. Run it a week or two into
a term.

**`nomad course setup`** in a terminal with no setting given asks: how
classes get access (the sources, with how many groups and environments here
are named like a course and how many instructors' own groups have people
active), which names are courses' (and what they match), the faculty group
(and its size), the term's first day, `min_students` and `names`; then shows
how many courses those settings make, what `discover` would suggest, and
what would change, and asks before writing (`--apply`: without asking).
Each setting is also an option (`--sources groups,environments`), for
scripts; `--ask` asks even then.

```
nomad course setup                           turn courses on: asks how classes get access
nomad course setup --term-start 2026-08-24   start a new term (or --sources, --pattern, ...)
nomad course discover                        what here may be a course, and what to do
nomad course list                            each course: instructors (and why), students
nomad course env GROUP ENV [--remove]        whoever used environment ENV is in GROUP's class
nomad course add GROUP NETID                 NETID teaches GROUP this term
nomad course exclude GROUP NETID             in GROUP and faculty, not teaching it this term
                                             (add, exclude: --every-term for good)
nomad course remove GROUP NETID              off either list
nomad course name GROUP "Econometrics"       how the course is shown
nomad course ignore GROUP [--undo]           named like a course, but isn't one
nomad course not-teaching NETID [--undo]     in the faculty group, teaches no course
nomad course answers                         what faculty members said this term
nomad course answer GROUP NETID --yes|--no   record what someone said
```

Like `nomad lab`, each change is shown first and written with `--apply`,
after a backup, only if the file still reads back as intended.

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
