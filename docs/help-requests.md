# Ask: help requests

*nomad 1.54.0, with the Console's fix 134.*

People ask the computing center for help from the Console's **Ask** page
(Help in the menu, or **Ask … about this job** on a job's page). A request
has

- a number from the one sequence the account requests use
  (`[console.tickets]`: `REQ-0042`), in every email's subject;
- what it is about (one of their jobs, their storage, software, an account,
  something else), a subject and their message;
- what they chose to attach, after reading each part in full: the job as
  nomad read it, the nodes it ran on, the end of its output (read from its
  cluster through the gate, as them, where Submit a job is open to them),
  their storage, and the page they came from. The parts are read again on
  the server when the request is sent, for that person only: nothing the
  page sends is taken as the job's record.

The center gets an email with the number in the subject and the person's
address as Reply-To; the person gets a copy saying the number. Everything
after that is kept with the request: the person's messages (each one opens
the request again), the center's answers (emailed to the person), and the
center's own notes (never shown or sent to the person). States: *waiting for
the center* → *answered* → *closed*. A person may close their own request.

Staff see every request under **All requests** on the same page, with what
was attached, and answer there.

## Every name is the site's

Nothing about one institution is written into nomad or the Console:

```toml
[support]
team = "RC"                        # "Ask RC", "RC answered" (default "Research Computing")
email = "hpc@example.edu"          # where requests go, unless [console.help] notify says

[console.tickets]
prefix = "REQ"                     # REQ-0042 (the default)

[console.help]
notify = ["hpc@example.edu"]
mail_domain = "example.edu"        # a person's address: NETID@example.edu
link = "https://nomad.example.edu/help"
per_hour = 5
# review_to = ["rc@example.edu"]   # while set, every email for a person goes here instead
```

## Where it is kept

In `lab_check.db` beside the hub's `combined.db` (the nomad account's,
readable by it only), with the account requests and the data cases: tables
`help_requests` and `help_messages`; every change in `lab_check_log`, and
each request sent and each answer in the Console's audit log first.

## Replies by email

A reply to one of these emails keeps the number in its subject but goes to
the center's mailbox, not back into the request: the conversation on the
Console is the one kept. Reading the mailbox back into the requests may come
later.
