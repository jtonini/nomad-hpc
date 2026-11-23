# Interactive Sessions

RStudio and Jupyter sessions on an interactive server: who has what open, how
much memory each holds, which are idle or stale, and acting on them.

## What is collected

The `interactive` collector, on the server itself, records each session at
every run (who, which kind, its memory and CPU) and keeps each person's day
and hour in small totals that are kept for good: sessions, time open, time
busy, CPU, memory, and the software environment each ran from. Courses and
the usage report are built from those totals.
[Collectors: interactive servers](collectors.md#interactive-servers-rstudio-jupyterhub)
has what each table holds and the settings.

## Reports

```bash
nomad report-interactive              # every session now, and what needs attention
nomad report-interactive --quiet      # only what needs attention
nomad report-interactive --json
```

It names users with many idle sessions, sessions idle longer than
`--idle-hours` (stale), and sessions holding more memory than
`--memory-threshold` (memory hogs).

## Acting on sessions

On the server, `nomad interactive` lists the sessions and acts on them. Each
is a dry run until `--apply`, and nothing changes until the server's
nomad.toml has `[actions] interactive = true`:

```bash
nomad interactive sessions                    # who has what open, and how long idle
nomad interactive stop PERSON PID --apply     # suspend an RStudio session, or stop a Jupyter server or kernel
nomad interactive clear --hours 48 --apply    # every session idle that long
nomad interactive restart jupyter --servers 12 --apply
```

An RStudio session is suspended with Workbench's own command, so it comes
back as it was; a Jupyter server or kernel is stopped as its person.
[Actions: an interactive server's sessions](actions.md#an-interactive-servers-sessions)
has the details, and how the same commands are started from the Console.

## In the Console

The Console's Interactive servers page shows, for staff, each server's
sessions now, by kind and by person, how long each has been idle, and what
needs attention (stale sessions, memory hogs, people with many idle
sessions). Where the actions are set up, staff start the commands above from
the Console, each shown as a dry run first.
