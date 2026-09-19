# Moving the PoC to a dedicated project AWS account

The one-box PoC at `poc.elenchus.chat` was stood up by hand
([`manual-poc.md`](manual-poc.md)) in a personal AWS account, where its
costs are mixed in with everything else on that bill. This is the
checklist for moving it into an AWS account that holds **only** the
Elenchus project — clean billing, and two people who can operate it.

It is written so the site is down for **a few minutes, once**, in a window
you choose. Nothing here is urgent: the current box keeps working until
the cutover in Part D.

**What moves, and how**

| Thing | Can it be moved? | So we… |
|---|---|---|
| The Lightsail box | No — an instance can't change accounts | rebuild it from [`manual-poc.md`](manual-poc.md) and restore the data (tens of MB) |
| The data (`/var/lib/elenchus`) | Yes — it's a directory | tar it on the old box, untar it on the new one |
| The `elenchus.chat` registration | Yes, free, between AWS accounts | transfer it (Part E) |
| The `elenchus.chat` hosted zone | No — [zones aren't transferred](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/hosted-zones-migrating.html) | recreate it and repoint the name servers (Part E) |
| The SES domain identity (DKIM) | No — it belongs to the old account's SES | create a new identity; new DKIM records (Part F) |
| The TLS certificate | Not worth it | let certbot issue a new one |
| The LLM API key | Deliberately not | re-enter it in the app's Settings; no secret travels between accounts |

Who does what: **[you]** needs a human — creating accounts and users,
handling credentials, anything that destroys data. **[cli]** can be done
from a terminal (or by Claude) once a CLI profile for the new account
exists. In the commands, `--profile elenchus` is the **new** account and
`--profile old` is the account the PoC lives in today.

---

## Part A — Decide first

- [ ] **[you] Who owns the account, and who pays?** If AWS costs are to be
      charged to the grant, the account should belong to the institution
      that holds it — root user on a **shared mailbox** (not a person's),
      institutional payment method. That also means it outlives any one
      person's involvement. The root email is awkward to change later;
      settle this before opening the sign-up page.
- [ ] **[you] Which region?** You are rebuilding the box anyway, so choose
      deliberately. The PoC is in `us-east-1` for no particular reason.
      If real participants might ever be hosted here, the data-protection
      review will likely want an **EU region** (`eu-west-1` Ireland,
      `eu-central-1` Frankfurt — Lightsail is in both). Whether the study
      runs on AWS at all is a separate decision
      ([cloud-deployment.md](../docs/cloud-deployment.md)); this only
      avoids a second move if it does. Below, `$REGION` is your choice.
- [ ] **[you] When?** Not in the run-up to anything that depends on the
      site. Parts B–C can be done any time; Part D needs a ~15-minute
      window with nobody mid-session.

## Part B — Create and secure the account

- [ ] **[you]** Create the AWS account with the shared mailbox as root.
- [ ] **[you]** Root user: strong password, **MFA on**, then put it away.
      Nobody works as root.
- [ ] **[you]** Give the two operators admin access. Either:
  - **Simple** — an IAM user each, `AdministratorAccess`, MFA required.
    Fine for two people and one small box.
  - **Recommended by AWS** — IAM Identity Center: one user each in an
    Administrators group with the `AdministratorAccess` permission set;
    short-lived credentials and a per-person audit trail. Granting access
    *to an AWS account* needs an **organization instance**, so switch on
    AWS Organizations in the new account first (free) —
    [an account instance can't do it](https://docs.aws.amazon.com/singlesignon/latest/userguide/account-instances-identity-center.html).
- [ ] **[you]** Turn on a **budget** with an email alert. The project
      should run at roughly **$8/month** (Part G), so $20 is a sensible
      tripwire.
- [ ] **[you]** On the machine that will run the rest, create a named CLI
      profile for the new account — `aws configure sso --profile elenchus`
      (Identity Center) or `aws configure --profile elenchus` (IAM user).
      From here on, **every command names its profile**; nothing should
      fall back to the default one.
- [ ] **[cli]** Check it: `aws sts get-caller-identity --profile elenchus`
      shows the **new** account id.

## Part C — Build the new box (no effect on the live site)

- [ ] **[cli]** Lightsail, in `$REGION`: Ubuntu 24.04, the ~$7/month plan
      (1 GB RAM), a **static IP** attached, ports **80** and **443** open
      (22 is open by default). This is [`manual-poc.md`](manual-poc.md) §1.
- [ ] **[cli]** SSH key for the new account's default key pair — write it
      straight to a `0600` file, and delete it when you're done:
      ```bash
      umask 077
      aws lightsail download-default-key-pair --profile elenchus --region $REGION \
        --query privateKeyBase64 --output text > ~/elenchus-new.pem
      ```
- [ ] **[cli]** Give the new box a **temporary name**, so it can be
      brought up and tested over HTTPS while the real name still points at
      the old one. In the *existing* zone:
      `poc-new.elenchus.chat  A  <new static IP>  TTL 60`.
- [ ] **[cli]** On the new box, follow [`manual-poc.md`](manual-poc.md)
      §3–§5 with these differences:
  - install the **same version the old box runs**
    (`curl -s https://poc.elenchus.chat/healthz` → `version`):
    `pip install "elenchus==X.Y.Z"` (if PyPI doesn't have that version
    yet, its "Release" workflow run is still waiting for approval);
  - leave `ELENCHUS_API_KEY` **empty** and let
    `ELENCHUS_SECRET_KEY=$(openssl rand -base64 36)` generate a **fresh**
    master key;
  - **skip** `elenchus admin create` — the accounts arrive with the data;
  - add `Environment=TZ=UTC` to the systemd unit
    ([OPERATIONS.md §4](../docs/OPERATIONS.md) explains why);
  - in the Nginx block and the certbot command use
    `poc-new.elenchus.chat` for now.
- [ ] **[cli] Rehearse the restore** with a copy of the live data. On the
      old box (this does not stop the service — a live copy can be
      mid-write, which is fine for a rehearsal and **not** for the real
      cutover):
      ```bash
      sudo tar -czf /tmp/rehearsal.tar.gz -C /var/lib elenchus
      ```
      Copy it across, then on the new box:
      ```bash
      sudo systemctl stop elenchus
      sudo rm -rf /var/lib/elenchus && sudo tar -xzf /tmp/rehearsal.tar.gz -C /var/lib
      sudo chown -R elenchus:elenchus /var/lib/elenchus
      sudo systemctl start elenchus
      ```
- [ ] **[cli] Verify** at `https://poc-new.elenchus.chat/healthz`: `status`
      `ok`, the same `version` and `schema_version` as the old box.
      `llm_configured` will be **`false`** — expected: the stored API key
      was encrypted under the old box's master key, so the server ignores
      it (and logs a warning) rather than failing.
- [ ] **[you]** Sign in on `poc-new` with an existing account and check the
      dialectics are there. Then **Settings → enter the LLM API key**;
      `/healthz` flips to `llm_configured:true`. Send one message to prove
      the round trip.

## Part D — Cut over (the only downtime)

Pick the window. Make sure no participant session is in progress (the
Study tab's roster shows any `tutorial` / `active` sessions).

- [ ] **[cli]** Old box: **stop the service first**, *then* archive — a
      consistent copy needs a quiet database:
      ```bash
      sudo systemctl stop elenchus
      sudo tar -czf /tmp/final.tar.gz -C /var/lib elenchus
      ```
      Leave the old service **stopped** from here on, so nothing can be
      written to the copy you are about to abandon.
- [ ] **[cli]** Copy `final.tar.gz` to the new box and restore it exactly
      as in the rehearsal.
- [ ] **[you]** The restore replaced the settings table, so **re-enter the
      LLM API key** in Settings once more.
- [ ] **[cli]** Repoint the real name — in the existing zone, change
      `poc.elenchus.chat  A` to the **new** static IP (TTL is 60 s).
- [ ] **[cli]** New box: add the real name to Nginx (`server_name
      poc.elenchus.chat poc-new.elenchus.chat;`), reload, then
      ```bash
      sudo certbot --nginx -d poc.elenchus.chat --redirect -n --agree-tos -m <ops mailbox>
      ```
      (certbot's HTTP check needs the A record to have propagated — give
      it a minute.)
- [ ] **[cli] Verify** `https://poc.elenchus.chat/healthz` — and that it is
      the **new** box answering: compare `systemctl show elenchus -p
      MainPID` on the new box with what you expect, or simply confirm the
      old service is still stopped while the site is up.
- [ ] **[you]** Sign in. Open the Study tab. Open an existing participant
      link: it must still work — links carry the host name, not the IP,
      so they survive the move.

**If anything is wrong:** point `poc.elenchus.chat` back at the old static
IP and `sudo systemctl start elenchus` on the old box. You are back where
you started within a minute, and nothing was lost — the old box has not
been touched beyond being stopped. This is why the old box is **not**
deleted until Part H.

## Part E — Move DNS and the registration (no downtime; any day after D)

The site keeps working throughout: DNS resolves normally while the zone
and the registration are
[in different accounts](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/domain-transfer-between-aws-accounts.html).

- [ ] **[cli]** New account: create a public hosted zone `elenchus.chat`.
      Note its four name servers.
- [ ] **[cli]** Recreate the records in it: `poc.elenchus.chat A` → new
      IP, and the `_dmarc` TXT. **Don't** copy the three
      `…._domainkey` CNAMEs — they belong to the old account's SES
      identity (Part F makes new ones). Drop `poc-new` unless you still
      want it.
- [ ] **[cli]** Check the new zone answers *before* switching to it:
      `dig +short poc.elenchus.chat @<one of the new name servers>`.
- [ ] **[cli]** Switch the domain to the new name servers (run in the
      account that currently holds the registration):
      ```bash
      aws route53domains update-domain-nameservers --profile old --region us-east-1 \
        --domain-name elenchus.chat --nameservers Name=ns-… Name=ns-… Name=ns-… Name=ns-…
      ```
- [ ] Wait **48 hours**. Name-server records are cached for up to two days;
      until then some resolvers still ask the old zone, so leave it alone.
- [ ] **[cli]** Transfer the registration (free; must be accepted within
      three days):
      ```bash
      aws route53domains transfer-domain-to-another-aws-account --profile old \
        --region us-east-1 --domain-name elenchus.chat --account-id <NEW ACCOUNT ID>
      # → prints a Password; then, in the new account:
      aws route53domains accept-domain-transfer-from-another-aws-account --profile elenchus \
        --region us-east-1 --domain-name elenchus.chat --password '<that password>'
      ```
- [ ] **[you]** In the new account, check the domain's contact details and
      that **auto-renew is on** (it renews each June, about $39).

## Part F — Email (only if/when the app sends real mail)

The PoC runs with `EMAIL_BACKEND=console`, so nothing depends on this yet.

- [ ] **[cli]** New account, SES in `$REGION`: create a **domain identity**
      for `elenchus.chat` with Easy DKIM; add the three CNAMEs it gives you
      to the **new** zone; wait for "verified".
- [ ] **[you]** Create SMTP credentials, put them in
      `/etc/elenchus/elenchus.env` on the box, set `EMAIL_BACKEND=smtp`,
      restart — [OPERATIONS.md](../docs/OPERATIONS.md) has the details. A
      new account's SES starts in the **sandbox** (verified recipients
      only) until production access is requested.

## Part G — What it should cost

| | per month |
|---|---|
| Lightsail, 1 GB plan | ~$7.00 |
| Route 53 hosted zone | $0.50 |
| DNS queries, SES at this volume | cents |
| **Total** | **~$8** — plus the domain renewal, ~$39 once a year |

LLM usage is billed by the model provider, not AWS; the app records every
call and its cost — `GET /api/admin/usage?days=30` (see "Cost and usage" in
the [Administration guide](../docs/administration.md)).

- [ ] **[cli]** After the first full month, check the new account's bill
      matches this. Anything else on it is a mistake.

## Part H — Retire the old resources

**Wait at least a week** after the cutover, and until Part E's 48 hours
are up. These steps destroy things — do them yourself, and look before
each one.

- [ ] **[you]** Take a last copy of `/var/lib/elenchus` and
      `/var/backups/elenchus` off the old box if you want them.
- [ ] **[you]** Old account: delete the Lightsail instance, then **release
      its static IP** — an unattached static IP is billed.
- [ ] **[you]** Old account: delete the old `elenchus.chat` hosted zone
      (only after the registration points at the new name servers and 48
      hours have passed) and the old SES identity.
- [ ] **[cli]** Confirm the old account's next bill has no Lightsail and no
      `elenchus.chat` zone on it.
- [ ] **[cli]** Delete `poc-new.elenchus.chat` if you kept it, remove the
      downloaded SSH key files, and update
      [`manual-poc.md`](manual-poc.md) if the region changed.
