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

For now they only read: none changes settings or collected data. Actions that
change something will come as a dry run followed by an apply, the way
`--apply` works on the command line.

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
| `usage.report` | hub | `nomad usage-report` (`from`, `to`, `cluster`, `min_cell`) |

How parameters are checked:
- **Numbers** must fall within their bounds.
- **Dates** must be real dates; a period can be at most five years and can't end after tomorrow.
- **Names** use letters, digits, `.`, `_` and `-` (plus `$` for a lab), and can't start with a dash.
- **Sites** must be among the hub's sites.
- **Unknown parameters** are refused, not ignored.

On the hub, actions that read data read the combined database. The usage
report writes into `~/.local/share/nomad/reports/` (readable only by its
owner). The Console offers those files by name and nothing outside that
directory.

`alerts` and `lab.show` can name people or groups, as the commands do. The
catalog marks them (`names`) so the Console shows them to administrators only.

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
