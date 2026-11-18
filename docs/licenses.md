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

    installer ALL=(root) NOPASSWD: /usr/lib/rstudio-server/bin/rstudio-server license-manager status
    installer ALL=(root) NOPASSWD: /opt/rstudio-connect/bin/license-manager status

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
(`nomad.toml.bak-licenses-add-...`), and reads it once. An entry of that name
already there, the same: nothing to do; different: `--replace`.

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

## In the Console

Staff see every license on the Licenses page: when each ends, in days, as
last read, with what was read, from where, when, and any problem; those not
being renewed at the end. The Home page says so when one has ended, ends
within 30 days, or can't be read.
