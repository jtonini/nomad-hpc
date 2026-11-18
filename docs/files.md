# My files: moving files from the browser

*nomad 1.55.0, with the Console's fix 135.*

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
                         #   "staff", "everyone" signed in with the campus sign-in, or "off"
max_mb = 2048            # the largest file moved from the browser
# piece_mb = 8           # a piece, 1 to 16
folders = ["~"]          # where My files opens, as shortcuts: ~ their home, {user} their name
# only_in_folders = false   # true: only in those folders and below

[console.files.sites.spydur]       # one cluster's own (optional)
folders = ["~", "/scratch/{user}"]
login = "spydur.example.edu"       # the host people use for rsync (default: its [[hub.sites]] host)
```

Every name is the site's; nothing about one institution is written into nomad
or the Console.

## Through the gate

`nomad-gate` (as the person, only with `serve --edit`):

| op | what |
|---|---|
| `put --path P --upload ID --offset N --total N --piece-sha256 H [--sha256 H] [--replace]` | one piece, on standard input (at most 16 MB); the last one with the whole file's sha256 |
| `put-status --path P --upload ID` | how much of it is there, what is at P, the room there |
| `put-cancel --path P --upload ID` | what was sent of it, removed |
| `get --path P [--offset N] [--length N]` | a piece of a file they can read, in base64 with its sha256 and the file's version; `--length 0`: only its size and version |

Two files move at once on a cluster's account; the rest of the gate's
requests are never kept waiting by them.
