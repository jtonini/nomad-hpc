# Lab membership check

Lab groups go on listing people long after they leave, and people work in a
lab without being in its group. Once a term, nomad asks each lab's PI who is
still in it, on **My lab** in the Console. Their answers become a to-do for
Research Computing, one cluster at a time. nomad never changes a group, an
account or a file itself.

## What a PI is asked

For each lab with a PI and someone to ask about:

- **Each member** in the lab's group now, on any site, the PI apart: still
  in my lab, has left, or not in my lab. For someone who has left, the PI
  also says what to do with their data: keep it for the lab, it can be
  removed, or ask me first.
- **Each person who sent jobs to one of the lab's condos** in the last
  `claim_days` without being in its group: in my lab, or a guest (another
  lab's member using the condo). A condo two labs share asks neither lab
  about the other's members.

Answers are kept as they are given; the PI sends the lab's check in when
every question is answered. Last term's answer is shown beside each
question.

Labs are found as the Console finds them (`[console.labs]`: `group_pattern`,
`leads`, `not_labs`; a course this term is not a lab), and their members as
it counts them. A lab's condos come from report.toml (see
[Condo reports](condo-reports.md)): a condo's `members` groups, else its
owners' groups. A condo's owners answer for its group too, which matters
for a group not named for anyone (`members = "group:chemlab$"`). A condo
given people by name or by Slurm account has no group to put anyone in, so
it doesn't ask about its users.

Who answers: the lab's PI (a staff member who leads a lab, for that lab),
or a Console administrator for them (kept as such). Not its research
assistants, and not operators for someone else's lab. An answer entered
with `nomad lab check answer` is kept as entered at the command line.

## What it leaves Research Computing to do

`nomad lab check todo`, and the Console's Admin pages, list each person's
latest answer as the change it calls for:

| Item | When | Done |
|---|---|---|
| remove | has left, or not in the lab, and in its group at a site since before that answer | once nomad's groups reading there no longer lists them, or marked done (kept on purpose) |
| add | in the lab, not in its group at a site where they used its condo | once the reading lists them, or marked done |
| data | has left, with what to do with their data, and where it is (storage_use) | when marked done |
| no_answer | the term's check closed with questions unanswered | once answered, or marked done |

Mark an item done with a note: `nomad lab check done LAB NETID KIND
[--site S] --note "..."` (NETID `-` for no_answer).

Each item comes from the answer given last for that person. Someone put
back in the group after they were said to have left is someone to ask
about again, not to take out. A mark made before the PI changed their
answer doesn't cover the new one.

## Email

`nomad lab check mail --send` sends what is due that day: one email a lab,
to each of its PIs (NETID@`mail_domain`), when the term's check opens and at
each reminder while the lab isn't done, until the check closes. While
`review_to` is set, every email goes there instead, saying whom it was for;
those are kept apart, so once `review_to` is cleared each PI gets their own,
the opening one first. Nothing is sent twice. While report.toml doesn't
read, nothing is sent unless `review_to` is set in `[console.labs.check]`
(report.toml's may be what holds the emails), and its condos are left out.

Without `--send` it only lists what is due; `--text` prints each email as it
would go. `--lab LAB --send` sends one lab's (to look at one in review); the
others stay due.

`nomad lab check timer --apply` (as root) installs a systemd timer that runs
it every morning as the nomad account:

```ini
# /etc/systemd/system/nomad-lab-check.service
[Service]
Type=oneshot
User=nomad
ExecStart=/opt/nomad/venv/bin/nomad -c /etc/nomad/nomad.toml lab check mail --send
```

## Settings

In the hub's nomad.toml:

```toml
[console.labs.check]
opens = ["09-15", "02-01", "06-15"]   # MM-DD: the day each term's check opens; [] (default): off
remind_days = [14, 28]               # days after it opens: reminders to PIs who haven't answered
closes_days = 42                     # days after it opens: unanswered, the lab is on the to-do
claim_days = 120                     # condo users of the last this many days are asked about
link = "https://nomad.example.edu/lab"   # where PIs answer, in the email
mail_domain = "example.edu"          # a PI's address: NETID@mail_domain
send_from = "Research Computing <rc@example.edu>"
review_to = ["rc@example.edu"]       # while set, every email goes here instead
# db = "/home/nomad/.local/share/nomad/lab_check.db"   # the answers (default: beside combined.db)
```

`mail_domain`, `send_from` and `review_to` not given here are report.toml's
`[report.labs]` (the condo reports'). `review_to = []` here sends to PIs
whatever report.toml says.

A term's check is its term's until the next one opens: answers are kept
after it closes, which only puts unanswered labs on the to-do and stops the
emails. It closes `closes_days` after it opens, or a week before the next
one opens if that is sooner.

Turning it on after this term's check would have closed puts every lab on
the to-do as unanswered at once (`nomad lab check status` says when it
closes): pick dates so that this term's is open, or turn it on after the
next one opens. Turned on, the check shows on My lab to each PI who signs in
straight away; emails go only when `mail --send` runs (the timer), and to
`review_to` while it is set.

## Where the answers are kept

In `lab_check.db` beside the hub's combined database (sync rewrites
combined.db, so not in it; a relative `db` is beside it too), readable by its owner only: every answer, who
gave it and when, and a log of every change. Run the commands as the
account the Console runs as (`sudo -u nomad nomad lab check ...`): run as
root, nomad refuses to make it, or to write one another account owns.

## Commands

| Command | |
|---|---|
| `nomad lab check status` | this term: each lab, how many asked about and answered, sent in, emails |
| `nomad lab check show LAB` | what the lab's PI is asked, and the answers so far |
| `nomad lab check answer LAB NETID ANSWER [--data D]` | keep an answer, as the PI would give it (`--by`) |
| `nomad lab check submit LAB` | the lab's check is done (every question answered) |
| `nomad lab check todo` | what the answers leave to do, a cluster at a time |
| `nomad lab check done LAB NETID KIND --note "..."` | mark a to-do item done |
| `nomad lab check mail [--send] [--lab LAB] [--text]` | the emails due today (`--text`: as they would go; `--lab`: one lab's) |
| `nomad lab check timer [--apply]` | send them every morning, from systemd |
