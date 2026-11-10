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
question, and when the person last sent a job to any cluster, most recent
first. Members with no job in a year, who were in the group before that, can
be marked as having left at once, with what to do with their data; any
answer can be changed after.

`not_sites` leaves out a site's groups altogether (members, condos, jobs
and the to-do): on a teaching server an instructor's group holds their
course's students, whom the PI is not the one to answer for.

Labs are found as the Console finds them (`[console.labs]`: `group_pattern`,
`leads`, `not_labs`; a course this term is not a lab), and their members as
it counts them. A lab's condos come from report.toml (see
[Condo reports](condo-reports.md)): a condo's `members` groups, else its
owners' groups. A condo's owners answer for its group when it has no PI of its own,
which matters for a group not named for anyone (`members =
"group:chemlab$"`); two labs sharing a condo each answer only for their
own. A condo
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

Each email greets the lab's PIs by name ("Dear Dr. Parish,"; `salutation`
sets how), taken from `[console.labs.check.names]`, else from the
account's full name (`getent passwd`, its GECOS field). A PI nomad has no
name for is greeted "Dear colleague,".

### The PIs' names

`nomad lab check names --set` adds the missing ones (run it in a terminal;
on a hub where nomad runs as its own account, `ssh -t`):

1. It looks for them on the system, read-only: the account's full name,
   then the directory, without a login (`[console.labs.check] directory`,
   else the system's `ldap.conf` URI and BASE; an anonymous `ldapsearch -x`
   for givenName and sn). It never uses a password; a directory that shows
   no one without a login is said so and left.
2. It shows what it found, for you to keep (all, none, or each).
3. It asks for the rest, one at a time: `First Last` (`First | Last` for a
   last name of more than one word, `Maria | de la Cruz`), Enter to leave
   it for later, or `-` when the group is not a lab at all (it goes to
   `[console.labs] not_labs`, as `nomad lab hide` does: no email, no lab
   page).
4. It shows the change to nomad.toml and writes it only when you say so:
   a backup first (`nomad.toml.bak-names-…`), the file's owner and mode
   kept, nothing written unless the file reads back with only those
   changes, nor over someone else's edit made meanwhile. It also says so
   when nomad.toml can be read by anyone on the machine.

`--from FILE` takes the answers from a file instead (the
`[console.labs.check.names]` table, or lines `NETID,First Last`; `-` for a
group that is not a lab; empty for later) and writes them with `--apply`.
`--no-lookup` skips step 1. For one PI later:
`nomad lab check name NETID First Last [--apply]` (`--remove` takes a name
out). `nomad lab check email LAB
[--reminder]` prints a lab's email as its PIs would get it now, whatever
has been sent. `nomad lab check names` shows how
each lab's email greets its PIs and where each name came from
(`--missing`: only those without one). The email ends with `signature`,
by default the Console's `[console.branding]` (its unit lines, then the
institution), the same lines as the sign-in page and the condo reports.

The opening email, for a lab with 132 members:

```text
Dear Dr. Parish,

Each term, Academic Research Computing asks the faculty who lead research groups to confirm who belongs to their labs. Your answers keep access to the clusters, condo nodes and research storage up to date.

For the Parish Lab (cparish$), please tell us whether each of its 132 members is still in the lab and, for anyone who has left, what should happen to their data.

You can answer at https://nomad.example.edu/lab until Monday, December 14, 2026. For a long list, the page can mark everyone with no job in the past year in one step.

Nothing happens automatically. Our team reviews your answers before making any change.

Thank you,

Academic Research Computing
Office of the Provost
Example University
```

A reminder starts "This is a reminder that the membership review for the
Parish Lab (cparish$) is still open: 120 people do not have an answer yet."

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
not_sites = ["spiderweb"]            # sites whose groups it leaves out (default: none)
salutation = "Dr. {last}"            # how a PI is greeted (default): {first}, {last} or {name}
names = { cparish = "Carol Parish", mdelacruz = { first = "Maria", last = "de la Cruz" } }
# signature = ["Research Computing", "Example University"]   # default: [console.branding]
# contact = "Questions: rc@example.edu"   # a line above the signature (default: report.toml's contact)
# date_format = "%Y-%m-%d"           # the closing day (default: "Monday, December 14, 2026")
# directory = { uri = "ldap://ldap.example.edu", base = "ou=people,dc=example,dc=edu", id = "uid" }
#                                    # where names --set looks names up, no login (default "auto": ldap.conf; false: never)
```

`names` is for PIs whose account has no full name, or a last name of more
than one word (as a table, `{ first = ..., last = ... }`). A name left
empty (`netid = ""`) counts as not there yet: `nomad lab check names
--missing` lists it.

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
| `nomad lab check email LAB [--reminder]` | the lab's email as its PIs would get it now, whatever was sent (nothing is sent) |
| `nomad lab check names [--missing]` | how each lab's email greets its PIs, and where each name comes from |
| `nomad lab check names --set [--from FILE [--apply]]` | find the missing names on the system, ask for the rest, write them |
| `nomad lab check name NETID First Last [--apply]` | one PI's name (`--remove` takes it out) |
| `nomad lab check timer [--apply]` | send them every morning, from systemd |
