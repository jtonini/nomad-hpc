# My files: moving files from the browser

*nomad 1.55.0, with the Console's fix 135; whole folders, copying between machines and lab workstations: nomad 1.58.0, fix 139.*

People put files on a cluster, and take files back, from the Console's **My
files** page: drag files onto the page (or **Choose files**), and they go to
the folder shown; **Download** beside a file brings it back. Each is done as
the person, through the cluster's nomad-gate, with their own permissions: a
file they couldn't write or read by logging in, they can't here either.

- A file goes in pieces (8 MB each), each checked on the cluster with its
  own fingerprint (sha256); when the last piece is there, the whole file is
  read back and its fingerprint checked against the one the browser worked
  out, and only then is it put in place, in one step. Until then it is a
  hidden file beside its place (`.NAME.nomad-upload-ID`), theirs alone.
- A break costs nothing: the network, a closed laptop, a Console restart.
  Dropping the same file on the same folder again goes on from where it
  stopped. What was sent of a file given up (**Cancel**) is removed; one left
  a week is removed when another file is sent to that folder.
- Never over a file that is there: the page asks **Replace?** first. A
  replaced file keeps its permissions, group and access lists.
- A new file is shared as its folder is: readable by the folder's group, or
  by anyone, only where the folder lets them in; written by its owner alone.
- Never a hidden file or folder (`~/.ssh`, `~/.bashrc`, ...), as given nor
  where a link leads, and never through a link.
- A file being taken back stops if it changes meanwhile (the browser says
  the download failed); each piece is checked too.
- Nothing is kept on the hub: each piece passes through. Every file sent or
  taken is in the Console's audit log, and in the cluster's syslog (its first
  and last piece).
- Up to 2 GB a file by default (`max_mb`). For bigger files, many files or
  whole folders, the page shows the `rsync` command to type on one's own
  computer (it goes on where it stopped too), and the `scp` one for Windows.

## Whole folders (fix 139)

A folder dropped on the page (or **Choose a folder**) goes whole: each of
its folders made there first (one there already is gone into), then each
file, as above. A file there already: **Replace**, **Skip** or **Keep both**
("name (2).ext"), once for all of them if they like. Hidden files and
folders, and links, are left out, and the page says how many.

## Copying between machines (1.58.0)

**Copy to...** beside a file or a folder copies it to a folder of theirs on
another machine: another cluster, or a lab workstation and back. The hub
does it, as the person on both, through each machine's gate: read in
pieces from the one (each checked), written in pieces on the other (each
checked), each whole file's sha256 checked there before it is put in place.
Nothing is kept on the hub.

- It goes on on the hub: the browser can be closed. **Copies** on the page
  says how far each is, and lists what wasn't copied and why. After a
  Console restart it goes on from the file it was at.
- A file there already by its name: left as it is (**Skip**), replaced
  (**Replace**: only a file of theirs), or both kept (**Keep both**: the new
  one as "name (2).ext"). A folder there already is gone into; with **Keep
  both** the copied folder gets a name of its own ("results (2)").
- Not started when it is too big for this way (`copy_max_gb`, 200 GB by
  default) or has too many files and folders (`copy_files_max`, 20 000):
  the page says to use rsync from one machine to the other. Nor when the
  machine it goes to hasn't room for all of it.
- A file that changes while it is being copied isn't copied (said so). A
  machine busy moving other files is waited for; one that doesn't answer is
  asked again over a minute and a half, then the copy stops and **Start
  again** goes on from where it stopped (and tries the files that failed).
- **Cancel** stops it between pieces: what was sent of the file it was at
  is removed, what was copied before stays.
- Two copies at once on the hub (`copies_at_once`), one a person; every
  copy asked, stopped and ended is in the audit log.

Each piece goes through the hub, one ssh request at a time, so this is for
the size of a project folder: many thousands of small files take a while.

```toml
[console.files]
copy = true              # false: no Copy to...
copy_max_gb = 200
copy_files_max = 20000
copies_at_once = 2
```

## Lab workstations: the gate alone (1.58.0)

A machine people keep files on that isn't one of the hub's sites (a lab's
workstations, and the network storage they mount) becomes one of My
files' machines with nomad's gate alone on it: no nomad for people there,
nor the libraries its other commands need (its own Python and nomad's files,
about 160 MB with uv).

1. Its account (a local system account, keys only, e.g. `nomad`), and as
   it the gate alone in a folder it owns:

   ```bash
   NOMAD_USERS_DIR=/opt/nomad-gate NOMAD_GATE_ONLY=1 bash install_nomad_for_users.sh 1.58.0
   ```

2. sudo for that account to act as people, through the gate only
   (`visudo -f /etc/sudoers.d/nomad-gate`):

   ```text
   Defaults:nomad !requiretty
   nomad ALL=(ALL,!root) NOPASSWD: /opt/nomad-gate/bin/nomad-gate run, /opt/nomad-gate/bin/nomad-gate run *
   ```

3. The hub's gate key let in, from the hub alone, as that account:

   ```bash
   /opt/nomad-gate/bin/nomad-gate install-key "$(cat gate_key.pub)" --from HUB-ADDRESS --edit --apply
   ```

4. On the hub, its host key in the nomad account's `known_hosts`, and an
   entry for it:

   ```toml
   [[console.files.machines]]
   name = "ws-kim"               # as people know it
   host = "ws-kim.example.edu"
   # gate_user = "nomad"         # the account of step 1 (default: nomad)
   # gate_jump = "zed@jumphost"  # reached only through that host
   folders = ["~", "/scratch/{user}", "/shared"]
   # groups = ["kim$"]           # who may use it (below)
   # workstation = "ws-kim"      # its name in nomad's workstation collector (default: host's first name)
   ```

5. Then, on the hub: `nomad gate test --machines --user YOU` asks each
   machine's gate as you (its version, half a file sent and removed, a
   folder listed); `--site NAME` asks one.

Who may use it from the Console: staff, and the members of its lab as
nomad's workstation collector knows it (the lab nomad last saw it in). Or,
said in its entry, the members of `groups`, or everyone My files is open to
with `groups = ["*"]`. A machine nomad knows no lab of, with no groups said:
staff only. The gate acts as the person, so no one sees more there than they
could logged in; this keeps people who can't log in there from seeing even
that.

Where several workstations mount the same home and storage, the gate on one
of them reaches it all.

## Turning it on

The cluster's say is the same as for saving scripts from the Console: its
hub key's line runs `nomad-gate serve --edit` (`nomad gate install-key
--edit`), and the hub's `[[hub.sites]]` entry for it says `edit = true`.
Where either is off, My files doesn't offer that cluster. The cluster's nomad
must be 1.55.0 or later (its nomad-gate does `put` and `get`).
`nomad gate test --site CLUSTER --user YOU` tries it, as you, leaving nothing
behind: the first half of a tiny file sent to your home (only its hidden part
is written), how much of it is there, that part removed, and a few bytes of
`/etc/os-release` taken back.

```toml
[console.files]
open = "submit"          # who: "submit" (whoever may Submit a job: [console] submit),
                         #   "staff", "everyone" signed in with the campus sign-in, "labs" (below), or "off"
# labs = ["kim$"]        # with open = "labs": staff, and these labs' people (1.60.0)
# active_days = 365      # with open = "labs": only those seen at work this lately; 0: all of them
max_mb = 2048            # the largest file moved from the browser
# piece_mb = 8           # a piece, 1 to 16
folders = ["~"]          # where My files opens, as shortcuts: ~ their home, {user} their name
# only_in_folders = false   # true: only in those folders and below

[console.files.sites.hpc1]         # one cluster's own (optional)
folders = ["~", "/scratch/{user}"]
login = "hpc1.example.edu"         # the host people use for rsync (default: its [[hub.sites]] host)
```

Every name is the site's; nothing about one institution is written into nomad
or the Console.

## Open to some labs first (1.60.0)

To open My files to a few labs before everyone, `open = "labs"` and the labs'
groups in `labs`. Besides staff, it is then open to each lab's people:

- its members now: in the lab's group on any site, as My lab counts them;
- anyone its PI said is in the lab on My lab's check (someone who used the
  lab's condo without being in its group: "in my lab");
- not those its PI said have left or aren't in the lab;
- and, with `active_days` (365 by default), only those nomad saw at work in
  that many days: a job on a cluster, a session on a lab machine, use of a
  login node or of a notebook server. A lab group goes on listing people long
  after they leave; this leaves them out until the PIs have said.

Once the PIs have said who is in their labs and the groups follow it,
`active_days = 0` opens it to all of each lab's people. The Console asks
again every ten minutes, so someone who starts working is let in soon after.

```
nomad files status [--names]                         who it is open to, and how many
nomad files open labs 'kim$' --active-days 365       what it would set, and how many it would open to
nomad files open labs 'kim$' --active-days 365 --apply
nomad files open staff --apply                       back to staff only (labs kept for another time)
```

On the hub, as the account the Console runs as (it reads the combined
database and the lab check's answers): `sudo -H -u nomad nomad -c
/etc/nomad/nomad.toml files status`. The lab machines offered stay as they
were: each to its own lab's people, and staff.

## Through the gate

`nomad-gate` (as the person, only with `serve --edit`):

| op | what |
|---|---|
| `put --path P --upload ID --offset N --total N --piece-sha256 H [--sha256 H] [--replace]` | one piece, on standard input (at most 16 MB); the last one with the whole file's sha256 |
| `put-status --path P --upload ID` | how much of it is there, what is at P, the room there |
| `put-cancel --path P --upload ID` | what was sent of it, removed |
| `get --path P [--offset N] [--length N]` | a piece of a file they can read, in base64 with its sha256 and the file's version; `--length 0`: only its size and version |
| `mkdir --dir P` | a new folder of theirs; one there already is said so (`exists`, and `folder` when it is one; 1.58.0) |
| `tree --dir P [--most N]` | what is under a folder, a folder before what is in it, each with its kind, size and time; hidden ones, links and other kinds left out and counted (1.58.0; at most 20 000) |

Two files move at once on a cluster's account; the rest of the gate's
requests are never kept waiting by them.
