# Account requests

A lab's PI asks Research Computing for accounts for their students on
**My lab** in the Console, and the request is kept: who asked, for whom, on
which clusters, and what became of each account, with who closed it and
when. It is the record a team with no ticket system doesn't otherwise have.
nomad never makes an account itself: Research Computing makes them as it
does now, and closes each one here.

## What a PI does

Only a lab's PI asks, and sees the lab's requests (Research Computing sees
every lab's). Not a research assistant, not an administrator for someone
else's lab, and not in View as: what a lab asks for is its PI's to ask. A
staff member who leads a lab asks for it like any PI. The card is on the
lab's card on My lab, so a PI whose lab group doesn't exist yet asks
Research Computing as before. On My lab, under **Request accounts**, the PI:

1. picks the clusters, one or several;
2. types or pastes the people, one per line: NetIDs (`jdoe`), addresses
   (`jdoe@example.edu`), or names (`Jane Doe`, `Doe, Jane`); or gives a
   table: CSV, tab-separated or Excel (.xlsx), with a header row naming its
   columns (NetID, Email, First name, Last name, or Name); a row pasted from
   a spreadsheet is read by its NetID, else its name;
3. checks them. nomad says who each one is, from the campus directory:

   | Shown | What it means |
   | --- | --- |
   | the person's name and NetID | found |
   | several people | that name is several people: the PI picks the one they mean |
   | too many of that name | give the NetID |
   | not found | no one in the directory by that NetID or name |
   | a surname alone | add the first name (a surname alone is hundreds of people) |
   | has an account on spydur | nomad has their jobs, logins or groups there already |
   | already asked for on spydur | an earlier request for them is still open |

4. sends the ones they mean, with a note if they like.

Research Computing is emailed (`notify`), with the PI's address to reply to.
Someone who never logged in nor ran a job isn't seen by nomad, so "has an
account" is a hint, not a promise either way.

## What Research Computing does

Under **Admin, Labs, Account requests**, each request lists its people and
clusters. When an account is made, mark it **made**; when it won't be,
**not made**, with why (needed). The PI is emailed either way, several
accounts closed at once in one email. When nomad already sees an account
that is still open (its groups reading, a login or a job), the list says so.

From the hub's shell, as the account the Console runs as:

```
sudo -u nomad nomad lab accounts list --open
sudo -u nomad nomad lab accounts close 12 jdoe spydur made
sudo -u nomad nomad lab accounts close 12 asmith spydur not_made --note "not enrolled this term"
sudo -u nomad nomad lab accounts lookup "Jane Doe" jdoe --file students.csv
sudo -u nomad nomad lab accounts check
```

## The directory

nomad looks people up with `ldapsearch` (openldap-clients), as a service
account. A campus directory usually answers nothing to an anonymous search,
and a name can only be looked up as an account that may read people's
entries. The password is kept in a file only the nomad account can read; it
goes to `ldapsearch` in a file of its own (`-y`) that is deleted after, never
on a command line, and only over `ldaps://`. nomad reads a person's NetID
(`uid`) and name (`displayName`, else `cn`, else given name and surname),
nothing else, and only of the NetIDs and names a PI typed.

```toml
[directory]
uri = "ldaps://ldap.example.edu"
base = "ou=people,dc=example,dc=edu"
bind_dn = "uid=svc-nomad,ou=special,dc=example,dc=edu"
password_file = "/home/nomad/.config/nomad/directory_password"   # the nomad account's, 600
timeout = 15
```

A host that already reads the directory through sssd with a service account
has what nomad needs in sssd.conf (`ldap_default_bind_dn`,
`ldap_default_authtok`): the hub's install kit can copy them into the two
places above, printing nothing of the password.

## Settings

```toml
[console.labs.accounts]
enabled = true                       # default: true once [directory] is set
sites = ["spydur", "arachne"]        # where a PI may ask (default: each site with Slurm nodes read this week)
notify = ["hpc@example.edu"]         # new requests are emailed here
most = 100                           # people in one request
link = "https://nomad.example.edu/diagnostics?tab=labs"   # the Admin page, in the email to ARC
```

The PIs' emails go as the [lab membership check](lab-check.md)'s do: from
`send_from`, to `NETID@mail_domain`, greeting the PI by `salutation` and
signed with `signature`. While `review_to` is set in `[console.labs.check]`,
they go there instead, saying whom each was for. The emails to `notify` are
Research Computing's own and always go there.

The requests are kept in lab_check.db, beside combined.db (the nomad
account's, readable by it alone), with every request and every close in its
log.
