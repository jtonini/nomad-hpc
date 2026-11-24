# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad db`: nomad's own databases (1.61.0) -- status, tidy, vacuum.
nomad.db_health says which databases and how."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import click

from nomad import db_health as H


def _ctx(ctx):
    config = (ctx.obj or {}).get("config") or {}
    return config, (ctx.obj or {}).get("config_target")


def _settings(config):
    return H.settings(((config.get("collectors") or {}).get("databases")) or {})


def _site_db(ctx, config):
    db = (ctx.obj or {}).get("db_path")
    return Path(db) if db else H.site_db(config)


def _history(site_db: Path):
    """The site's own database, for the readings kept (None: not there yet)."""
    try:
        if not site_db.exists():
            return None
        c = sqlite3.connect(f"file:{site_db}?mode=ro", uri=True, timeout=10)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'db_files'").fetchone():
            c.close()
            return None
        return c
    except sqlite3.Error:
        return None


@click.group("db")
def db_group():
    """nomad's own databases: how big each is and how fast it grows, old
    copies, and the space a vacuum gives back.

    \b
    nomad db status [--tables] [--sites]   sizes, growth, free space (--sites: every site's, on the hub)
    nomad db tidy [FILE...] [--apply]      old copies and empty databases removed (an old copy only
                                           when the live database holds everything in it)
    nomad db vacuum [FILE] [--full] [--apply]   free space given back
    nomad db keep [--apply] [--no-vacuum]  older readings rolled up by the keep rules (1.63.0)
    """


@db_group.command("status")
@click.option("--tables", is_flag=True, help="Each table's size too (reads every page: minutes for gigabytes).")
@click.option("--sites", is_flag=True, help="On the hub: every site's databases, as the sites last read them.")
@click.pass_context
def db_status(ctx, tables, sites):
    """Each of nomad's databases here: its size, how fast it grows, the free
    space inside it (what a vacuum gives back) and on its disk, and old
    copies or empty databases beside it."""
    config, _ = _ctx(ctx)
    s = _settings(config)
    if sites:
        return _sites(config)
    site = _site_db(ctx, config)
    hist = _history(site)
    found = H.files(config, s, site=site)
    if not found:
        click.echo("No nomad database here.")
        return
    grew = False
    try:
        for path, role in found:
            try:
                r = H.measure(path)
            except (OSError, sqlite3.Error) as e:
                click.echo(f"{path} ({role}): not read ({e})")
                continue
            g = H.growth(hist, str(path)) if hist else None
            grew = grew or g is not None
            line = (f"{path} ({role}): {H.size(r['bytes'])}"
                    + (f" + {H.size(r['wal_bytes'])} write-ahead log" if r["wal_bytes"] > 1e6 else ""))
            if g is not None:
                line += f", {'grows' if g >= 0 else 'shrank'} {H.size(abs(g))} a day (last 7 days)"
                if g > 0:
                    line += f", the disk full in about {r['disk_free'] / g / 365:.0f} year(s) at that" \
                        if r["disk_free"] / g > 730 else f", the disk full in about {r['disk_free'] / g:.0f} days at that"
            click.echo(line)
            click.echo(f"    free inside (a vacuum gives it back): {H.size(r['free_bytes'])}; "
                       f"vacuuming: {r['auto_vacuum']}; free on its disk: {H.size(r['disk_free'])}; "
                       f"written {r['modified_at'].replace('T', ' ')}; {len(r['tables'])} tables")
            if role in ("old copy", "empty"):
                click.echo(f"    {'an old copy' if role == 'old copy' else 'empty'}: nomad db tidy")
            if tables and role not in ("empty",):
                _tables(path, r, hist)
    finally:
        if hist is not None:
            hist.close()
    if not hist:
        click.echo("(Growth needs the databases collector's readings: it runs with nomad collect.)")
    elif not grew:
        click.echo("(No growth yet: the databases collector reads every few hours, and growth shows "
                   "once its readings span a day.)")


def _tables(path, r, hist):
    t0 = time.time()
    sizes = H.table_sizes(path)
    rate = H.table_growth(hist, str(path)) if hist else {}
    rows = {t["name"]: (t["max_rowid"] or 0) - (t["min_rowid"] or 0) + (1 if t["max_rowid"] else 0)
            for t in r["tables"]}
    names = sorted(rows, key=lambda n: -(sizes or {}).get(n, 0) if sizes else -rows[n])
    click.echo(f"    {'table':32} {'size':>10} {'rows (about)':>14} {'rows a day':>11}")
    for n in names[:20]:
        click.echo(f"    {n[:32]:32} {H.size(sizes.get(n)) if sizes else '-':>10} {rows[n]:>14,} "
                   f"{(f'{rate[n]:,.0f}' if n in rate else '-'):>11}")
    if len(names) > 20:
        click.echo(f"    ... {len(names) - 20} more")
    click.echo(f"    ({'sizes measured' if sizes else 'no sizes: this SQLite has no dbstat'} in "
               f"{time.time() - t0:.0f} s; rows a day from the collector's readings, last 7 days)")


def _sites(config):
    hub = H.hub_db(config)
    if not hub or not hub.exists():
        raise click.ClickException("--sites is for the hub ([hub] lists sites, its combined database here)")
    c = sqlite3.connect(f"file:{hub}?mode=ro", uri=True, timeout=30)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(db_files)")]
        if "source_site" not in cols:
            click.echo("No site has sent its databases' readings yet (nomad 1.61.0 at the sites, then a sync).")
            return
        # 1.63.0: what each site found at its latest reading (one time a run), so a database
        # removed since isn't listed from its last reading before
        rows = c.execute("SELECT f.source_site, f.path, f.role, f.bytes, f.free_bytes, f.disk_free, f.timestamp "
                         "FROM db_files f JOIN (SELECT source_site, MAX(timestamp) AS t FROM db_files "
                         "GROUP BY source_site) m ON f.source_site = m.source_site AND f.timestamp = m.t "
                         "ORDER BY f.source_site, f.bytes DESC").fetchall()
        for site, path, role, b, free, disk, ts in rows:
            g = H.growth(c, path, site=site)
            click.echo(f"{site:14} {Path(path).name:28} {role:9} {H.size(b):>10}  "
                       f"{(('+' if g >= 0 else '') + H.size(g) + '/day') if g is not None else '-':>14}  "
                       f"free inside {H.size(free):>9}  disk free {H.size(disk):>10}  read {ts[:16].replace('T', ' ')}")
    finally:
        c.close()


@db_group.command("tidy")
@click.argument("files", nargs=-1, type=click.Path(dir_okay=False))
@click.option("--apply", is_flag=True, help="Remove them (otherwise only say what would go).")
@click.pass_context
def db_tidy(ctx, files, apply):
    """Old copies and empty databases removed: an old copy (FILE, or one
    found beside nomad's databases) only when the live database (the site's,
    or the hub's for a hub's copy) holds everything nomad keeps that it
    holds; an empty one only when nomad doesn't use it."""
    config, _ = _ctx(ctx)
    s = _settings(config)
    site = _site_db(ctx, config)
    hub = H.hub_db(config)
    found = H.files(config, s, site=site)
    used = {p for p, role in found if role in ("site", "hub", "helper")}
    if files:
        todo = []
        for f in files:
            p = Path(f).expanduser().resolve()
            if p in used:
                raise click.ClickException(f"{p} is one nomad uses: not removed")
            todo.append((p, "empty" if H._rows_in(p) == 0 else "old copy"))
    else:
        todo = [(p, role) for p, role in found if role in ("old copy", "empty")]
    if not todo:
        click.echo("No old copy or empty database beside nomad's.")
        return
    back = 0
    for p, role in todo:
        if not p.is_file():
            click.echo(f"{p}: not there")
            continue
        gb = H.size(sum(Path(str(p) + x).stat().st_size for x in ("", "-wal", "-shm") if Path(str(p) + x).exists()))
        if role == "empty":
            verdict = {"ok": True, "notes": ["it holds no rows"], "short": []}
        else:
            live = hub if (hub and H.is_hub_copy(p)) else site
            if not live or not Path(live).exists():
                click.echo(f"{p} ({gb}): kept: no live database here to check it against")
                continue
            verdict = H.compare(Path(live), p)
        click.echo(f"{p} ({gb}, {role}):")
        for n in verdict["notes"]:
            click.echo(f"    {n} (as meant)")
        if not verdict["ok"]:
            click.echo("    KEPT: only in it: " + "; ".join(verdict["short"][:12]))
            continue
        if apply:
            back += H.remove(p)
            click.echo("    removed")
        else:
            click.echo("    would be removed (--apply)")
    if apply and back:
        click.echo(f"{H.size(back)} given back.")


@db_group.command("vacuum")
@click.argument("file", required=False, type=click.Path(dir_okay=False))
@click.option("--full", is_flag=True, help="Once: set it for incremental vacuuming and vacuum it whole.")
@click.option("--apply", is_flag=True, help="Do it (otherwise only say what it would give back).")
@click.pass_context
def db_vacuum(ctx, file, full, apply):
    """The free space inside a database given back (FILE, else the site's).
    Set for incremental vacuuming, it moves pages, never rows, and the
    databases collector does it once a week by itself. --full does it once:
    the database set for that and vacuumed whole (needs about twice its size
    free beside it, for SQLite's copy and journal, which it writes there;
    holds the database for the while; the hub then copies this site's
    database whole at its next sync, since a whole vacuum may renumber rows)."""
    config, _ = _ctx(ctx)
    p = Path(file).expanduser().resolve() if file else _site_db(ctx, config).resolve()
    if not p.is_file():
        raise click.ClickException(f"{p}: not there")
    r = H.measure(p)
    click.echo(f"{p}: {H.size(r['bytes'])}, free inside {H.size(r['free_bytes'])}, vacuuming: {r['auto_vacuum']}, "
               f"free on its disk {H.size(r['disk_free'])}")
    if not full and r["auto_vacuum"] != "incremental":
        click.echo("Not set for incremental vacuuming: --full sets it, once (see nomad db vacuum --help).")
        return
    if full and r["disk_free"] < H.room_needed(r["bytes"]):
        raise click.ClickException(f"a whole vacuum needs about {H.size(H.room_needed(r['bytes']))} free beside "
                                   f"the database ({H.size(r['disk_free'])} free)")
    if not apply:
        click.echo(f"Would give back about {H.size(r['free_bytes'])}"
                   + (" (a whole vacuum, then incremental from there; the hub copies it whole once)" if full else "")
                   + ". Run again with --apply.")
        return
    t0 = time.time()
    if full:
        try:
            before, after, had_log = H.full(p)
        except H.VacuumError as e:
            raise click.ClickException(f"not vacuumed, nothing changed: {e}")
    else:
        (before, after), had_log = H.incremental(p), False
    took = time.time() - t0
    site = _site_db(ctx, config)
    try:
        c = sqlite3.connect(str(site), timeout=30)
        try:
            H.ensure(c)
            H.vacuum_record(c, p, "full" if full else "incremental", before, after, took, datetime.now())
            c.commit()
        finally:
            c.close()
    except sqlite3.Error:
        pass
    click.echo(f"{H.size(before)} -> {H.size(after)} ({H.size(max(0, before - after))} given back, {took:.0f} s).")
    if had_log:
        click.echo("Its change log for the hub was taken out: the hub copies this database whole at its next "
                   "sync, then goes on as before.")


@db_group.command("keep")
@click.option("--apply", is_flag=True, help="Do it (otherwise only say what is due).")
@click.option("--no-vacuum", is_flag=True, help="Leave the space it frees inside the database.")
@click.pass_context
def db_keep(ctx, apply, no_vacuum):
    """The keep rules on the database nomad collects into here (1.63.0):
    readings older than detail_days (90) rolled up -- CPU per core and disk
    per device to hours, then days past hourly_days (365); lab machines'
    mounts to what each day was; collector runs to a count a day -- so
    history stays, at less detail. The tables reports read over terms and
    years (1.64.0) keep full detail report_detail_days (395), then one
    reading every sample_minutes (15); sessions on interactive servers
    (where their daily totals are kept) and programs run on lab machines are
    then removed. Jobs, groups, alerts and usage are never trimmed
    (docs/databases.md).

    \b
    The first time it removes many rows: each table with more than
    keep_max_rows due is taken out of the change log first (the hub copies
    it whole at its next sync), then the database is vacuumed (as nomad db
    vacuum --full, the first time: the hub then copies it whole once). After
    that the databases collector does it once a day."""
    from datetime import date

    from nomad import keep_rules as K
    config, _ = _ctx(ctx)
    raw = ((config.get("collectors") or {}).get("databases")) or {}
    k = K.settings(raw)
    for p in k.problems:
        click.echo(f"note: {p}")
    site = _site_db(ctx, config).resolve()
    hub = H.hub_db(config)
    if hub and site == Path(hub).resolve():
        raise click.ClickException(f"{site} is the hub's combined database: it follows the sites, which keep "
                                   "their own (run this where nomad collects)")
    if not site.is_file():
        raise click.ClickException(f"{site}: not there")
    conn = sqlite3.connect(str(site), timeout=60)
    try:
        today = date.today()
        due = K.todo(conn, k, today)
        covered = K.covered(conn)
        click.echo(f"{site}: {H.size(site.stat().st_size)}")
        any_due = False
        waiting = []

        def how(lv):
            if lv == "hourly":
                return "to hours"
            if lv == "daily":
                return "to days"
            if td.rule.kind == K.SAMPLE:
                return f"to one reading every {k.sample_minutes} minutes"
            if td.rule.kind == K.SESSIONS:
                return f"to one reading every {k.sample_minutes} minutes, and where sessions end or begin"
            return "removed" if td.rule.kind == K.REMOVE else "rolled up"
        for td in due:
            d, hr = k.days(td.table)
            if not td.present:
                continue
            what = td.rule.what
            if td.table not in covered:
                waiting.append(td.table)
            if not td.levels:
                when = (f"; its first day, {td.first}, is due {td.first + timedelta(days=d)}"
                        if td.first is not None else "")
                click.echo(f"  {td.table} ({what}): nothing due (full detail {d} days{when})")
                continue
            any_due = True
            spans = "; ".join(f"{how(lv)}, {a} to {b - timedelta(days=1)}" for lv, a, b in td.levels)
            click.echo(f"  {td.table} ({what}): {td.rows:,} rows due ({spans})")
        if waiting and not apply:
            click.echo(f"New here, left by the daily run until nomad db keep --apply goes through them once: "
                       f"{', '.join(waiting)}.")
        if not any_due:
            click.echo("Nothing due.")
            if apply:
                K.run(conn, k, "by hand", today)        # noted: the collector goes on from here daily
            return
        if not apply:
            big = [td.table for td in due if td.rows > k.max_rows]
            if big:
                click.echo(f"With --apply, taken out of the change log first (more than {k.max_rows:,} rows due; "
                           f"the hub copies them whole at its next sync): {', '.join(big)}.")
            click.echo("Run again with --apply.")
            return
        big = [td.table for td in due if td.rows > k.max_rows]
        out = K.untrack(conn, big)
        if out:
            click.echo(f"Taken out of the change log (the hub copies them whole at its next sync): {', '.join(out)}")
        t0 = time.time()
        done = K.run(conn, k, "by hand", today)
        for d in done:
            click.echo(f"  {d['table']}: {d['before']:,} -> {d['after']:,} rows ({d['days']} days, "
                       f"{d['seconds']:.0f} s)")
        click.echo(f"Gone through in {time.time() - t0:.0f} s.")
    finally:
        conn.close()
    if no_vacuum:
        click.echo("Not vacuumed (--no-vacuum): the space stays free inside the database, for its new readings.")
        return
    r = H.measure(site)
    t0 = time.time()
    if r["auto_vacuum"] == "incremental":
        before, after = H.incremental(site)
        kind, had_log = "incremental", False
    elif r["disk_free"] >= H.room_needed(r["bytes"]):
        try:
            before, after, had_log = H.full(site)
        except H.VacuumError as e:
            click.echo(f"Not vacuumed, nothing changed by it: {e}. The readings are rolled up all the same; the "
                       "space they freed stays inside the database, for its new readings.")
            return
        kind = "full"
    else:
        click.echo(f"Not vacuumed: a whole vacuum needs about {H.size(H.room_needed(r['bytes']))} free beside the "
                   f"database ({H.size(r['disk_free'])} free). The readings are rolled up all the same.")
        return
    took = time.time() - t0
    try:
        c = sqlite3.connect(str(site), timeout=30)
        try:
            H.ensure(c)
            H.vacuum_record(c, site, kind, before, after, took, datetime.now())
            c.commit()
        finally:
            c.close()
    except sqlite3.Error:
        pass
    click.echo(f"Vacuumed ({kind}): {H.size(before)} -> {H.size(after)} ({took:.0f} s).")
    if had_log:
        click.echo("Its change log for the hub was taken out: the hub copies this database whole at its next "
                   "sync, then goes on as before.")
