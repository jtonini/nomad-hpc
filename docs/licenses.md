# Licenses

Licensed software stops working on the day its license ends, and the first
to notice is usually someone whose work stopped. nomad reads when each
license ends, where the software itself keeps the date, lists them all in
one place, and emails the computing center before each one does. It never
changes a license.

- Each machine reads its own licenses (the `licenses` collector, a few times
  a day) and keeps one row a license (`license_status`).
- The hub gets the rows with everything else (`nomad sync`), lists them
  (`nomad licenses`, the Console's Licenses page) and emails the center
  (`nomad licenses remind --send`, every morning: `nomad licenses timer`).

## What nomad reads

| kind | where the date is | settings |
|---|---|---|
| `posit` | Posit's `license-manager status` (Workbench, Connect, Package Manager), as root through `sudo -n` | `program` (default `/usr/lib/rstudio-server/bin/rstudio-server`), `sudo` (default true) |
| `flexlm` | a FlexLM license file (Ansys, Lumerical, MATLAB's network license, ...): when each feature ends | `file` (a pattern too: `/opt/x/license_files/*.lic`) |
| `gurobi` | a `gurobi.lic` (a token server's, or a machine's): its EXPIRATION | `file` |
| `cert` | the web certificate a server presents | `host`, `port` (default 443) |
| `command` | what a command prints (Mathematica's `$LicenseExpirationDate`, a vendor's tool) | `command` (a list, the program's full path first), `pattern` (optional: a regular expression, its first group the date), `timeout` |
| `date` | a date typed in: a contract, a site license that came by email; `never` for a perpetual one | `ends` |

Only the date is kept, and a few words about it (Posit's Status line, how
many FlexLM features and how many are permanent, a Gurobi license's type).
Nothing else a program prints is kept or shown: no keys, no license file's
signatures or host IDs, no licensee.

**FlexLM**: the license ends when the first feature still in force does. A
renewal usually comes as a new file with a new name: with `file` a pattern
(`*.lic`), the new file is read with the old, a feature's latest end counts,
and nothing needs changing. A feature that has ended and is in no newer file
is counted as ended (the list says how many). A file that only points at a
license server (`SERVER`, `USE_SERVER`) has no dates: list the server's own
file, on the server.

**Gurobi**: a token server's `gurobi.lic` has the end; a machine's file that
only names the token server doesn't, nor does a web license (WLS: Gurobi's
site shows it).

**Posit**: `license-manager status` needs root. The account nomad collects
as gets one sudo line for that command and nothing else, for example in
`/etc/sudoers.d/nomad-licenses` (with `visudo -f`):

    hpcadmin ALL=(root) NOPASSWD: /usr/lib/rstudio-server/bin/rstudio-server license-manager status
    hpcadmin ALL=(root) NOPASSWD: /opt/rstudio-connect/bin/license-manager status

## Listing them

On the machine that can read each license (its nomad.toml):

    [collectors.licenses]
    enabled = true
    # min_hours = 6                         # read at most this often

    [[collectors.licenses.items]]
    name = "Posit Workbench"
    kind = "posit"
    about = "RStudio in the browser; renewed through Posit"

    [[collectors.licenses.items]]
    name = "Ansys and Lumerical"
    kind = "flexlm"
    file = "/opt/ansys_inc/shared_files/licensing/license_files/*.lic"

    [[collectors.licenses.items]]
    name = "Web certificate"
    kind = "cert"
    host = "nomad.example.edu"

    [[collectors.licenses.items]]
    name = "Gaussian 16"
    kind = "date"
    ends = "never"
    about = "perpetual site license"

    [[collectors.licenses.items]]
    name = "Posit Connect"
    kind = "posit"
    program = "/opt/rstudio-connect/bin/license-manager"
    renew = false                           # not being renewed: listed, never reminded

Every entry may say `about` (what it is, who renews it, a contract number)
and `remind_days` (its own, in place of the hub's). A license that isn't
being renewed keeps `renew = false`: it stays on the list, marked so, and no
reminder goes.

The commands do the same without editing by hand, on that machine:

    nomad licenses find                     what it seems to have, as `add` lines
    nomad licenses add "Posit Workbench" posit --about "RStudio in the browser"
    nomad licenses add "Posit Workbench" posit --about "RStudio in the browser" --apply
    nomad licenses renew "Posit Connect" no --apply
    nomad licenses read                     read them now, and say what was read

`add` shows the entry first; `--apply` writes it (with `[collectors.licenses]
enabled = true` if that isn't set), keeping a copy of the file before
(`nomad.toml.bak-licenses-add-...`), and reads and keeps it at once (so
does `renew --apply`): the hub has it after its next sync, not hours later
at the collector's next reading. An entry of that name already there, the
same: nothing to do; different: `--replace`.

`find` looks for Posit's programs, FlexLM and Gurobi license files in the
usual software folders (never under /home; `--root FOLDER` for others) and
the web certificates on ports 443, 8787 and 3939 here, and prints each as an
`add` line. Name each as people know it before adding it.

## Reminders

On the hub:

    [licenses]
    remind_days = [60, 30, 14, 7, 1]        # an email this many days before each ends
    to = ["hpc@example.edu"]                # default: [support] email
    # send_from = "nomad@example.edu"       # default: [mail] from
    # link = "https://nomad.example.edu/licenses"   # default: [alerts] link + /licenses
    # db = "/home/nomad/.local/share/nomad/licenses.db"   # what was sent (default: beside combined.db)

    nomad licenses remind --text            what is due now, and the email (nothing is sent)
    nomad licenses remind --send            send it
    sudo nomad -c /etc/nomad/nomad.toml licenses timer --apply   every morning at 08:20, as nomad

What is due goes in one email:

- **a reminder whose day came**: each once. A license first read 5 days
  before its end gets one email, not five; a renewal moves the end, and the
  reminders start over.
- **a license that ended** in the last 30 days, once (one that ended long
  before nomad read it is only listed).
- **a license nomad can't read** for 2 days (why, and the end it last read),
  once until it is read again.
- **a machine whose readings are 3 days old** (is nomad collecting there?),
  once.

The emails name licenses and machines, never people, and link to the
Console's list.

## Whether a program runs on the lab machines

A license can be in force and still not cover a machine. A program licensed
machine by machine (Q-Chem's node-locked license) refuses to start on a
workstation added after its license was made, and its license file says
nothing a script can read about which machines it covers. So nomad tries:
twice a day, on each workstation a site collects from, a tiny job with each
install of each program listed. For Q-Chem it is two hydrogen atoms,
Hartree-Fock, the smallest basis: a few seconds. It runs as an unprivileged
account (`run_as`, the gate's `nomad`; `nobody` where there is none), in a
folder under /tmp that is removed after.

On the site that reaches the machines (the workstation collector's):

    [collectors.license_tries]
    enabled = true
    # min_hours = 11              # try at most this often (twice a day)
    # machines = []               # default: [collectors.workstation] workstations;
    #                             # "slurm": every node of this cluster (below)
    # skip = []                   # machines left out (a node in repair)
    # ssh_user = "root"           # the account ssh logs in as on each machine
    # run_as = "nomad"            # the account each tiny job runs as (else nobody)
    # timeout = 120               # seconds one tiny job may take
    # at_once = 6                 # machines tried at the same time

    [[collectors.license_tries.programs]]
    name = "Q-Chem"
    recipe = "qchem"              # how to try it (nomad knows: qchem)
    min_version = "6.4"           # older installs are left out (6.1, 6.3, ...)
    # roots = ["/opt", "/usr/local", "/software", "/apps", "/usr/local/sw", "/share/apps"]

    nomad licenses tries setup Q-Chem qchem --min-version 6.4 [--nodes] [--apply]   list it (shown first)
    nomad licenses tries now          try now on every machine (or --machine HOST), and keep it
    nomad licenses tries              what ran where at the last try (on the hub: every site's)

For each machine and install, one of:

- **runs**;
- **license**: the program refused its license, and why in a few words:
  "the license expired on 12 Sep 2026", "this machine isn't in the license";
- **doesn't start**: "its setup says it is in /usr/local/qchem70, which isn't
  on this machine";
- **still running** after `timeout`, or **failed** (its last line).

Q-Chem's installs are the folders named qchem* (or q-chem*) two levels under
the `roots` that hold `qcaux/license/qchem.license.dat` and a `bin/qchem` (a
folder that only holds the versions isn't tried); a folder's version
is the digits in its name (qchem64: 6.4, qchem611: 6.1.1), and those older
than `min_version` are left out (6.4 leaves out 6.1, 6.1.1 and 6.3; once
left out, their rows go at the next try). Nothing the
program prints is kept beyond those words: its license keys and the
machine's ids are never stored or sent. A machine that doesn't answer keeps
its last results, marked not reached.

The daily license email (`nomad licenses remind`) says each change once: a
program that doesn't run on a machine (the first email lists every one
then), and one that runs there again. In the Console, staff see every
machine on the Licenses page, and each lab's people see their own machines
on their lab's page.

### On a cluster's nodes

The same tries on every compute node of a cluster, the condo nodes too (a
license made for the nodes there when it was bought may not cover a node
added later). On the cluster's head node, in the site's nomad.toml:

    [collectors.license_tries]
    enabled = true
    machines = "slurm"            # every node Slurm knows, asked again at each round
    # skip = ["node17"]           # left out
    # ssh_user                    # default: the account nomad runs as here

`nomad licenses tries setup Q-Chem qchem --min-version 6.4 --nodes --apply`
writes both. The nodes are reached over ssh as that account, which needs to
be let in on a node without a job there (where Slurm's pam_slurm_adopt keeps
people out of nodes they have no job on, it can't: then list it in
`skip`, or try lab machines only). The tiny job runs outside Slurm, on one
core, for a few seconds. When Slurm gives no nodes, nothing is tried and
nothing kept is dropped.

## In the Console

Staff see every license on the Licenses page: when each ends, in days, as
last read, with what was read, from where, when, and any problem; those not
being renewed at the end. The Home page says so when one has ended, ends
within 30 days, or can't be read.
