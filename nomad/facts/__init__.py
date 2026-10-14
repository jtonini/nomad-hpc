"""Facts: nomad's answers to three questions, as data any front can show.

    sites(conn, days)                     every site, now, and its jobs over days
    person(conn, username, days, ...)     one person's work
    labs(conn, config, netid, days, ...)  the labs a person leads
    courses(conn, config, netid, ...)     the courses a person teaches, this term
    questions(conn, config, netid, ...)   the courses to ask a faculty member about

Read-only, from a nomad database (a site's own, or the hub's combined.db),
whatever its tables hold: a figure that rests on nothing measured is None,
and what was not measured at all is left out, never shown as zero. Each
answer is a dict with "api" (nomad.facts/1), "kind", "generated_at", the
window and "notes" saying what the figures mean.

These functions decide nothing about who may ask: callers do. The Console
answers /api/v1/me for whoever is signed in, /api/v1/lab for the labs they
lead, and /api/v1/sites (aggregates only) for anyone signed in; `nomad
facts` answers whoever can read the database. See docs/facts.md.
"""
from nomad.facts._common import API, open_readonly  # noqa: F401
from nomad.facts.courses import courses, may_answer, questions  # noqa: F401
from nomad.facts.labs import labs  # noqa: F401
from nomad.facts.person import person  # noqa: F401
from nomad.facts.sites import sites  # noqa: F401
