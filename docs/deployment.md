# Deploying the monitor as an unattended service

What this gets you: three source checks a day, a Monday digest, reports produced when the data
behind them are fit to report on, and an email to whoever is on the distribution list. All times are
Asia/Baku.

What it will not do until you say so: send anything. Delivery is off in configuration and has no
credentials, so every run composes the message, records the intent and reports that it was not sent
and why. That is deliberate — it makes the whole path testable before an account exists.

---

## 1. What runs, and when

| Task | When (Asia/Baku) | What it does |
|---|---|---|
| `source-check` | 09:15, 13:15, 17:15 daily | Refresh CBA and SSC; produce whatever is now due; deliver it |
| `weekly-digest` | Monday 08:30 | Digest of the **previous calendar week**, Monday to Sunday |
| `monitor` | 07:45, 18:45 daily | Missed runs, quiet sources, deliveries awaiting a person. Silent when healthy |

The times live in `config/schedule.yaml`, which the application reads, and in the systemd timers,
which cannot read YAML. `monitor schedule check` compares them and `deploy/install.sh` refuses to
install units that disagree — a report that silently stops arriving is otherwise very hard to notice.

### What triggers a report

A report is produced when its trigger fires **and** its readiness rules pass. Nothing is produced on
a calendar alone, and an unchanged input produces no new edition however ready it is.

| Report | Trigger | Held back until |
|---|---|---|
| Monthly monitor | A new verified banking month | Required CBA tables have arrived; companions too, or 7 days have passed (then it publishes, labelled partial); no critical data-quality failure |
| Sector review | The monthly edition was published | Its own inputs are present and the sources are not quiet |
| MPR brief | A Monetary Policy Review is released | Released within 45 days; at least 40 passages; most of them verified |
| FSR brief | A Financial Stability Report is released | Released within 60 days; at least 80 passages; most verified |
| Decision update | A rate decision is released | Released within 10 days; the decision parsed into a rate and an announcement date |

Two rules deserve naming, because both are easy to get wrong and expensive when wrong:

**Staleness is measured from the last time a source published, not from the period the data
describe.** The July banking tables appear in late August, so a healthy monthly series always
reports on a period several weeks old. Judging by period end would call every series stale in the
days before its next release.

**Recency is measured from the release date, never from when the system first downloaded it.** A
backfill loads years of archive in a single run. None of it is news, and none of it triggers a brief.

---

## 2. Install

Requirements: a Linux host with Docker and the Compose plugin, about 4 GB of RAM and 20 GB of disk,
and outbound HTTPS to `www.cbar.az`, `uploads.cbar.az`, `www.stat.gov.az` — plus
`login.microsoftonline.com` and `graph.microsoft.com` once email is on. Nothing inbound.

```bash
sudo git clone <this repo> /opt/az-macro-banking
cd /opt/az-macro-banking
sudo ./deploy/install.sh          # builds the image, checks the schedule, installs the timers
```

The installer leaves the timers **stopped**. Before starting them:

```bash
cd /opt/az-macro-banking/deploy

# 1. put a dataset in place - either restore a backup into the volume, or build one:
docker compose run --rm cli backfill --start 2020-01          # ~30 minutes, ~600 MB

# 2. see what the first real run would do, without doing it:
docker compose run --rm cli run-task --task source-check --dry-run

# 3. start the timers:
sudo systemctl enable --now azmonitor-source-check.timer azmonitor-weekly.timer azmonitor-monitor.timer
systemctl list-timers 'azmonitor-*'
```

### State

Everything that must survive lives in one Docker volume, `azmonitor-state`:

```
/var/lib/azmonitor/
  data/monitor.sqlite        observations, documents, publications, passages, decisions
  data/deliveries.sqlite     what was sent, to whom, and what the provider said
  data/raw/                  the original bytes of every downloaded file
  data/backups/              VACUUM INTO snapshots, taken before each run
  data/state/                run history, last-run summaries, the job lock
  outputs/<type>/<edition>/  every version of every report, immutable once written
  outputs/latest/<type>      pointer to the current version, repointed only after success
```

Back up the volume. The dataset can be rebuilt from the sources if it is lost; **the record of what
has already been sent to people cannot**, which is why `deliveries.sqlite` lives beside it.

To sync to object storage instead of relying on host backups, set `AZMONITOR_RESTORE_CMD` and
`AZMONITOR_SAVE_CMD` in `.env`. They run inside the job lock, so restore, work and save are one
protected unit: a failed restore stops the run before anything is produced (exit 5), and a failed
save leaves the outputs on disk and says so (exit 6).

---

## 3. Turning on email

1. **Register the application.** In Entra ID, create an app registration. Grant the **application**
   permission `Mail.Send` (not delegated) and have an administrator consent to it. Create a client
   secret and note its expiry — a secret that quietly expires is the most common way this stops
   working.

2. **Scope it.** `Mail.Send` as an application permission lets the app send as *any* mailbox in the
   tenant. Restrict it to the one sender with an application access policy:

   ```powershell
   New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId azmonitor-senders@yourbank.az `
       -AccessRight RestrictAccess -Description "azmonitor may send only as the reporting mailbox"
   ```

3. **Fill in `.env`** (`chmod 600`, never committed):

   ```
   AZMONITOR_GRAPH_TENANT_ID=...
   AZMONITOR_GRAPH_CLIENT_ID=...
   AZMONITOR_GRAPH_CLIENT_SECRET=...
   AZMONITOR_GRAPH_SENDER=azmonitor@yourbank.az
   AZMONITOR_OWNER_EMAIL=you@yourbank.az
   ```

4. **Switch delivery on** in `config/delivery.yaml`: `enabled: true`.

5. **Test with a real edition but no schedule:**

   ```bash
   docker compose run --rm cli delivery send --report monthly --dry-run   # composes, sends nothing
   docker compose run --rm cli delivery send --report monthly             # actually sends
   docker compose run --rm cli delivery status
   ```

The message carries an executive summary drawn from the report's own validated findings, the main
PDF as an attachment, and links to the rest. Nothing is written at delivery time: a sentence that was
not good enough for the deck does not appear in the covering email either.

### Links to the other files

Set `AZMONITOR_ARCHIVE_BASE_URL` and run the archive service to make the links resolve:

```bash
docker compose --profile archive up -d archive     # serves outputs/ read-only on 127.0.0.1:8081
```

Put it behind the reverse proxy and authentication the host already has before pointing anything at
it. With the variable unset, emails carry the attachment and name the other files without linking
them — a dead link in a board pack is worse than no link.

---

## 4. WhatsApp (optional)

Off by default, and it stays off until three separate things are true, because a message to someone's
personal phone should be harder to enable than a message to their inbox:

1. an official provider account (Meta Cloud API or Twilio) with credentials in `.env`;
2. templates approved by the provider under the names in `config/delivery.yaml`, with their
   variables in the **same order** — the order is positional, and a mismatch sends the wrong words;
3. `whatsapp_opted_in: true` against that recipient in `config/delivery.yaml`, with the date the
   opt-in was recorded.

A WhatsApp message is a notification that a report is ready. No figures are sent over it.

---

## 5. Running it

```bash
cd /opt/az-macro-banking/deploy

docker compose run --rm cli schedule status     # readiness, staleness, missed runs, next window
docker compose run --rm cli schedule check      # do the timers still match the config?
docker compose run --rm cli delivery status     # sent, pending, needing a person
docker compose run --rm cli status              # the dataset itself

journalctl -u azmonitor-source-check -n 100     # what the last check did
systemctl list-timers 'azmonitor-*'             # when the next one is
```

### Exit codes

| Code | Meaning | Worth an alert? |
|---|---|---|
| 0 | Fine, whether or not anything was produced | no |
| 2 | Partial: some datasets failed; prior outputs preserved | if it repeats |
| 3 | Failed | yes |
| 4 | Another run held the lock | no — that is the design working |
| 5 | Restore failed; nothing was processed | yes |
| 6 | Work succeeded, saving state back failed | yes |

---

## 6. When a delivery is not certain

A provider call has three outcomes, and the third is the reason the delivery ledger exists.

**Sent** — acknowledged. Never sent again.
**Failed** — refused, clearly. Retried within the configured attempts and backoff.
**Needs review** — a timeout, a dropped connection, a 5xx *after* the request was accepted, a
success with no message id, or a run that died mid-send. The message may or may not have gone out.

Ambiguous deliveries are **never retried automatically**. Retrying is how a board receives the same
pack twice, so a person decides:

```bash
docker compose run --rm cli delivery status      # lists them with the provider's own words

# after checking the sent-items folder or the provider's console:
docker compose run --rm cli delivery resolve --id <id> --resolution delivered     --by t.hasanov
docker compose run --rm cli delivery resolve --id <id> --resolution not_delivered --by t.hasanov
```

`delivered` closes it. `not_delivered` returns it to the queue to be attempted again.

---

## 7. Recovery

The failure cases and what to do about each are in
[`operations.md`](operations.md#recovery-bringing-the-monitor-up-on-a-machine-that-has-nothing),
which covers restoring onto a machine that has nothing, a corrupt database, a blocked edition and a
lock left by a dead process. In a container deployment the commands are the same, prefixed with
`docker compose run --rm cli`.

The short version: a failed run never replaces a good report. Editions are written to a new directory
and `outputs/latest` is repointed only after a successful render, so whatever broke, the last good
edition is still the one anybody reading the archive will find.

---

## 8. AI narrative (optional, and not required for anything)

Every report is produced with no API of any kind: the deterministic path writes descriptive text,
and the analyst path takes a JSON narrative validated claim-by-claim against the fact pack. The API
path exists only if you want drafting help, and it is configured explicitly:

```yaml
# config/delivery.yaml
ai_narrative:
  enabled: true
  limits:
    max_calls_per_run: 2
    max_calls_per_day: 8
    max_usd_per_day: 5.00
    max_usd_per_month: 50.00
  on_limit_reached: fall_back_to_facts_only
```

with `ANTHROPIC_API_KEY` and `AZMONITOR_NARRATIVE_MODEL` in `.env`. The run tracks spend against the
ceilings and, on reaching one, falls back to the facts-only narrative and labels the edition as such
rather than exceeding the limit. Whatever it produces is still validated against the fact pack before
it can reach a slide; an ungrounded sentence is rejected exactly as an analyst's would be.

---

## 9. What is not configured here

These need a decision or an account and cannot be prepared from the code side:

| Item | Needed for | Who supplies it |
|---|---|---|
| Linux host with Docker | Everything | Infrastructure |
| Entra ID app registration, `Mail.Send`, admin consent, client secret | Email | IT / tenant administrator |
| Sending mailbox and an application access policy scoping the app to it | Email | IT |
| Recipient addresses | Email | You, in `.env` |
| A URL and authentication for the archive | Links in emails | Infrastructure |
| WhatsApp provider account and approved templates | WhatsApp | You, if you want it |
| Object storage credentials | Off-host backups | Infrastructure, if wanted |
| Anthropic API key and spending limits | AI narrative | You, if you want it |

Everything else — collection, analysis, validation, report production, scheduling, the delivery
ledger, monitoring and recovery — is implemented, tested and ready to run without any of the above.
