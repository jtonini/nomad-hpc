# nomad's own databases

nomad keeps what it collects in SQLite databases: each site's, the hub's
combined one, and a few helpers beside them (`lab_check.db`, `notices.db`, ...).
Since 1.61.0 it watches them too: how big each is, how fast it grows, the
space a vacuum would give back, and old copies left beside them.

## The databases collector

On by default wherever nomad collects. Every `every_hours` (6) it reads each
database here:

- its size, its write-ahead log, the free space inside it (what a vacuum
  gives back) and the free space on its disk;
- each table's highest row number, so the rows each table writes a day can be
  told without reading the tables (measuring a table's bytes reads every page:
  minutes for a database of gigabytes).

The readings (`db_files`, `db_tables`) go to the hub with the rest, so the
Console's Infrastructure page shows every site's databases. It warns (an
alert, `database_growth`) when a database grew more in a day than
`alert_growth_mb` (1 GB) and, once it has a week of days, more than
`growth_factor` (4) times its usual day: a collector writing far more than it
should is found the day it starts. A disk filling up is the disk collector's
alert already (its forecast).

```toml
[collectors.databases]
# enabled = true
# every_hours = 6
# stray_days = 14            # another .db not written this long is an old copy
# alert_growth_mb = 1024     # a day's growth that warns (and growth_factor times the usual)
# growth_factor = 4
# incremental_vacuum = true  # weekly, where a database is set for it
# keep_days = 400            # these readings
```

Which databases: the site's (`[database] path`, else `nomad.db` in
`[general] data_dir`), the hub's combined one where `[hub]` lists sites, the
helpers, and any other `*.db` in those folders. One not written for
`stray_days` is an **old copy** (a copy made before an upgrade or a restore);
one with no rows at all is **empty**.

## `nomad db status`

```
nomad db status              each database here: size, growth a day, free inside and on its disk
nomad db status --tables     and each table's size and rows a day (slower)
nomad db status --sites      on the hub: every site's, as each last read them
```

## `nomad db tidy`

Removes old copies and empty databases. An old copy goes only when the live
database (the site's, or the hub's for a hub's copy) holds everything nomad
keeps that it holds:

- every job and every measured job (a pending array's placeholder, an id with
  `[..]`, is replaced by the array's own tasks: counted, not held against it);
- a table that keeps one row per thing (groups, monthly usage, job accounting)
  by what each row is about, not by its date (a membership that ended since is
  counted, not held against it);
- the lab machines' process records one by one;
- any other table with a time column: at least as many rows up to the copy's
  last day; tables nomad trims on purpose (temperatures, raw samples) aside.

Otherwise the copy stays and what only it holds is said. Without `--apply`
it only says what would go. `nomad db tidy FILE` checks one file; one that
nomad uses is never removed.

## `nomad db vacuum`

SQLite doesn't give back the space of deleted rows by itself. Set for
*incremental* vacuuming, a database gives it back page by page, moving pages,
never rows, and the collector does it once a week. Setting a database for it
takes one whole vacuum:

```
nomad db vacuum --full            what it would give back
nomad db vacuum --full --apply    once: set for incremental vacuuming, vacuumed whole
nomad db vacuum --apply           from then on, by hand (the collector does it weekly)
```

A whole vacuum needs about the database's size free on its disk and holds the
database while it runs (minutes for gigabytes: a collection or a hub's sync
then waits or tries again). It may also give rows new numbers, which the
hub's incremental sync goes by, so it takes the site's change log out: the hub
copies that site's database whole at its next sync, then goes on as before.

## Keeping less

What nomad keeps of each kind of reading, and for how long, is next (summaries
kept for good, the detail for a while); until then nothing is removed but old
copies and empty databases.
