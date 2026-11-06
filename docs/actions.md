# Actions

An action is one nomad command that the Console (or `nomad actions run` on
the hub) may start, on the hub or on a site. Actions come from a fixed
catalog: each has its own command words and parameters, which are checked
before anything runs. Nothing reaches a shell, and nothing outside the catalog
can be asked for.

```bash
nomad actions list                              # the catalog
nomad actions run collectors --site c1          # on a site, through its agent
nomad actions run collectors                    # on the hub (every site's, from combined.db)
nomad actions run usage.report -p from=2025-10-01 -p to=2026-10-07 -p cluster=c1
nomad actions run syscheck --here               # on this host, as a site runs it
```

## The catalog

All of them read, but two (1.31): `node.drain` and `node.resume` change a
node's state in Slurm, and only when their `apply` parameter is true;
otherwise they are a dry run of it, the way `--apply` works on the command
line. See [Draining and resuming a node](#draining-and-resuming-a-node).

| Action | Runs on | What it is |
|---|---|---|
| `version` | hub, sites | `nomad version` |
| `config.check` | hub, sites | `nomad config check` |
| `collectors` | hub, sites | `nomad collectors` (`days`) |
| `syscheck` | sites | `nomad syscheck` |
| `status` | hub, sites | `nomad status` |
| `alerts` | hub, sites | `nomad alerts --unresolved` (`severity`) |
| `per_user` | hub, sites | `nomad per-user --mask` (`days`) |
| `insights.brief` | hub | `nomad insights brief` (`site`, `hours`) |
| `console.roles` | hub | `nomad console roles --mask` (`netid`) |
| `lab.show` | hub | `nomad lab show` (`lab`) |
| `storage.use` | hub, sites | `nomad storage use` (`server`, `path`, `over`, `top`, `days`) |
| `sync.status` | hub | `nomad sync status` |
| `usage.report` | hub | `nomad usage-report` (`from`, `to`, `cluster`, `min_cell`) |
| `node.drain` | sites | `nomad node drain --json` (`node`, `reason`, `by`, `apply`) |
| `node.resume` | sites | `nomad node resume --json` (`node`, `reason`, `by`, `apply`) |

How parameters are checked:
- **Numbers** must fall within their bounds.
- **Dates** must be real dates; a period can be at most five years and can't end after tomorrow.
- **Names** use letters, digits, `.`, `_` and `-` (plus `$` for a lab), and can't start with a dash.
- **Paths** are absolute (`/home`), of letters, digits and `. _ / + @ , : = -` only.
- **Sizes** are written as `--over` takes them: `500G`, `1T`, `2TB`.
- **Sites** must be among the hub's sites.
- **Notes** (a node's reason) are one line of printable characters, at most
  200, without `"` or `\`. They are passed as one word (`--reason=TEXT`), so
  none can be read as an option.
- **Who asked** (`by`) is a NetID. The Console fills it in with the
  administrator signed in, whatever the page sent; `nomad actions run` fills
  it in with whoever typed it (`SUDO_USER` through sudo).
- **`apply`** is true or false. Only true adds `--apply`.
- **Unknown parameters** are refused, not ignored.

On the hub, actions that read data read the combined database. The usage
report writes into `~/.local/share/nomad/reports/` (readable only by its
owner). The Console offers those files by name and nothing outside that
directory.

`alerts`, `per_user`, `lab.show` and `storage.use` can name people or groups,
as the commands do. The catalog marks them (`names`) so the Console shows them
to administrators only.

## How the hub reaches a site

The hub uses a key of its own, not the key `nomad sync` uses. A site lets that
key run one program only, `nomad agent`:

```
restrict,command="cd / && exec /usr/bin/env -u PYTHONHOME -u PYTHONPATH /path/to/python -m nomad.cli agent" ssh-ed25519 AAAA... nomad-actions@hub
```

- **`command=`:** whatever the connection asks for, sshd runs the agent. The
  agent reads one request (JSON, at most 64 KB), checks it against the
  site's own catalog, runs it, and answers. The site never relies on the
  hub's checking.
- **`restrict`:** no terminal and no port, agent or X11 forwarding.

The result is that whoever holds the hub's key, the hub included, can ask a
site for catalog actions and nothing else. If the hub is compromised, the
sites are exposed only to those actions.

A site whose `host` is `localhost` (collectors on the hub itself) needs no
key: its actions run on the hub directly, as the account the Console or
`nomad actions` runs as.

On the hub's side, the ssh command:
- offers this key first and never reuses a shared connection (a master opened
  with a stronger key would otherwise carry the request);
- refuses unknown host keys;
- asks for no terminal.

Each answer says whether sshd ran the agent as the key's forced command. If
it didn't, the hub doesn't use the answer and says why. That happens when the
key was added by hand without `command=`, or when ssh fell back to another of
the hub's keys named in `~/.ssh/config` because the site doesn't have this one.
`[actions] allow_unrestricted = true` in the hub's nomad.toml accepts such
answers, which is not advised.

The hub treats a site's answer as data:
- **Size:** it reads at most 16 MB and stops the connection beyond that.
- **Fields:** it keeps only the known fields, with their expected types and sizes.
- **Files:** it never takes a list of files from a site.

On the site:
- **One action at a time.** One more request may wait up to 30 seconds; any
  others are refused at once.
- **Silent connections close.** A connection that sends no request within 10
  seconds is closed.
- **Every request is logged,** refused ones too, one JSON line each in
  `~/.local/share/nomad/agent.log` (readable only by its owner): time, source
  address, action, parameters, exit status.
- **The interpreter runs from `/`.** `cd /` keeps a stray module in the home
  directory from being imported.

## Setting it up

On the hub, as the account that runs the Console and `nomad sync`:

```bash
nomad actions key
```

It makes the key the first time (`~/.config/nomad/agent/id_ed25519`, or
`[actions] agent_key` in nomad.toml) and prints the line to run on each site.

On each site, as the account nomad runs as there:

```bash
nomad agent install-key 'ssh-ed25519 AAAA... nomad-actions@hub'           # what would change
nomad agent install-key 'ssh-ed25519 AAAA... nomad-actions@hub' --apply
```

- It adds the line above to `~/.ssh/authorized_keys`, with a backup.
- It uses the site's own Python, so the agent is the same nomad its
  collectors run.
- An existing line holding the same key is replaced.
- `--from 10.0.0.5` also limits where the key may connect from.

Then, from the hub:

```bash
nomad actions run version --site SITE
```

The sites are those `nomad sync` reads (nomad.toml `[hub]`, or sync.toml), and
their host keys must already be known, as they are once sync has run.

## Draining and resuming a node

`node.drain` stops new jobs starting on a node; the jobs running there run to
their end. `node.resume` lets a drained (or down) node take jobs again. They
are `nomad node drain` and `nomad node resume` on the site, which an
administrator there can also run by hand:

```bash
nomad node drain node07 --reason "disk errors"            # a dry run: what would happen
nomad node drain node07 --reason "disk errors" --apply    # do it
nomad node resume node07 --apply
```

Without `--apply` nothing changes. The dry run shows:
- the node's state as Slurm has it now, and its reason (who set it, when);
- the jobs running there (`squeue`);
- the one `scontrol update` command that would run;
- whether sudo would let it run (`sudo -n -l`, which runs nothing).

With `--apply`, that command runs, and the node is read again to say what
Slurm made of it. It is done only when Slurm shows the change.

**Who runs scontrol.** Slurm lets only its administrators drain a node. Set in
the nomad.toml of the account the site's agent runs as (installer, zeus or
nomad, say):

```toml
[actions]
slurm_as = "slurm"     # as Slurm's own account, through sudo
# slurm_as = "self"    # as this account, when it is a Slurm administrator itself
```

Nothing changes until it is set. With `"slurm"`, that account needs one sudo
line, which root adds (`visudo -f /etc/sudoers.d/nomad-node`). The dry run prints
it with the account's name and scontrol's path:

```
zeus ALL=(slurm) NOPASSWD: /usr/bin/scontrol update NodeName\=* State\=DRAIN Reason\=*, /usr/bin/scontrol update NodeName\=* State\=RESUME
```

It lets the account update nodes as Slurm's account, and run no other
scontrol command (sudo's `*` also matches spaces, so other node settings
could follow; jobs, partitions and the controller are out of reach). The
agent holds the hub to the catalog: through it, the hub can ask for these
two commands on one node, and nothing else.

**What runs it is checked too.** Before anything is changed (and in the dry
run), nomad checks that no one but root and this account could change the
nomad running here: its Python, and every folder Python imports from, with
the folders above them. That code would run scontrol as Slurm's account. A
pass is taken as true for ten minutes (`~/.local/share/nomad/install.ok`).

**One environment.** sudo drops `SLURM_*` settings for the change, so every
Slurm program here runs without them (`SLURM_CONF`, `SLURM_CLUSTERS`): what
is read is the cluster that is changed. A cluster whose slurm.conf is found
only through `SLURM_CONF` can't be changed this way.

**What Slurm keeps.** Slurm records the account that ran scontrol as the
reason's author (`slurm`, through sudo from the Console; by hand in a
terminal, the login name), so the reason itself says who asked:
`disk errors (jtonini via nomad)`. Who asked is what the hub's request says
(the Console's signed-in administrator): a site trusts the hub's key for it.
Slurm drops a node's reason when it resumes, so a reason given to `resume`
goes to the logs only.

**What decides "done".** After the change, the node is read again. Drained:
Slurm shows it drained, with this reason. Resumed: no longer drained, down
or failing (not responding yet is how a node that was down comes back).
When scontrol takes too long, Slurm's state still decides; if it doesn't
show the change yet, the answer is "not known yet".

**Logs.** Each change, and each refusal to make one, goes to syslog
(authpriv, as `nomad-node`) and to `~/.local/share/nomad/nodes.log`. The
agent's own log has every request too. In the Console, the audit log has who
started it, before it runs.

**How it is checked.**
- The node is one name, as Slurm names it (no ranges or lists, not ALL).
- Slurm must know it (`scontrol show node`).
- A node that isn't drained, down or failing has nothing to resume.
- scontrol and squeue are run by their full paths (found on PATH, then in
  `/usr/bin`, `/usr/local/bin`, `/opt/slurm/bin`), never through a shell.
  They don't run at all when someone else could have put them there (a
  folder above them others may write, say).
- sudo is looked for in the system's folders only.

## For the Console

```python
from nomad import actions
actions.catalog()                            # every action, with each parameter's kind, bounds and choices
actions.targets()                            # {"hub": True, "sites": ["c1", "c2"]}
actions.run("collectors", {"days": 7}, site="c1", cancel=event, on_output=callback)
actions.report_file(name)                    # a file the usage report wrote, by bare name
```

`run` returns:
- `ok`, `exit_code`, `output` and `errors` (each kept up to 1 MB), `seconds`;
- `timed_out` and `cancelled`;
- `files`, for an action that writes them;
- for a site, also `restricted` and the site's `nomad_version`.

It raises `ActionError` when the request itself is refused.

- **Output is text from the site.** Show it as text, never as HTML.
- **`on_output`** streams output as it comes for actions on the hub. A
  site's output arrives when its action ends.
- **Cancelling** stops the hub's side at once. A site may still finish the
  action on its own, within the action's time limit.
- **One usage report at a time.** `files` lists what changed in the reports
  directory, so two reports at once would list each other's files.
- **Opening a report file:** use `actions.open_report_file(name)`. It refuses
  a file that was replaced by a link after the check.
- **Actions that change something** (`read_only: false`) name their apply
  flag (`apply`). Run them first without it and show what the dry run says;
  set it only after the administrator confirms. Fill each `caller`
  parameter from the signed-in administrator yourself, never from the page.
  `node.drain` and `node.resume` print JSON (`ok`, `problems`, `notes`,
  `before`, `jobs`, `command`, `after`, `said`); a request refused before
  anything ran prints `ok: false` and `error`.
