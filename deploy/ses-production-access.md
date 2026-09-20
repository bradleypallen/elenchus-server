# Requesting Amazon SES production access

A new Amazon SES account is in the **sandbox**: it delivers only to
addresses that have been verified with AWS, 200 messages a day. For
Elenchus that means invitations, password-reset links and login links
reach nobody but the account owner. The platform says so when it happens
(the Invites tab, the System tab, an alert) — but the fix is to ask AWS to
move the account out of the sandbox. This is the request, ready to paste,
and what to have in place first so that everything it says is true.

Production access is granted **per AWS account, per region**. If the PoC
moves to a dedicated account ([account-migration.md](account-migration.md),
Part F), the request has to be made again there — this text is written to
be reused.

> Check the current state any time:
> `aws sesv2 get-account --region us-east-1 --query ProductionAccessEnabled`

## Before you submit

AWS reviews these by hand and declines vague ones. They look for three
things: that you only mail people who expect it, that you notice bounces
and complaints, and that the numbers are plausible. Two of those need a
little preparation.

- [ ] **Run Elenchus 0.8.2 or later.** Until 0.8.2 the public *email me a
      login link* form emailed **whatever address was typed into it**,
      registered or not. In the sandbox that was harmless — SES refuses
      strangers. With production access it would let anyone make your
      domain email arbitrary people: unsolicited mail, bounces and
      complaints against `elenchus.chat`, and grounds for AWS to suspend
      sending. From 0.8.2 a login link goes only to an active, registered
      account, at most five times in fifteen minutes, and the form answers
      identically either way. **Do not switch production access on before
      this is deployed.** (`/healthz` shows the running version.)
- [ ] **Send bounce and complaint notices somewhere a person reads them.**
      Today they go nowhere: SES forwards them by email to the message's
      return address, `no-reply@elenchus.chat`, and that domain has no
      mailbox (no MX record). In the SES console: **Identities →
      `elenchus.chat` → Notifications → Feedback notifications → Edit** —
      for **Bounce** and for **Complaint**, create an SNS topic (e.g.
      `elenchus-ses-feedback`), tick *include original email headers*, and
      save. Then **SNS → Topics → that topic → Create subscription →
      Email**, with the administrator's address, and **confirm it from the
      email AWS sends**. (The console sets the topic's permissions for you;
      by CLI it is `aws sns create-topic` / `aws sns subscribe`, then
      `aws ses set-identity-notification-topic --identity elenchus.chat
      --notification-type Bounce|Complaint --sns-topic <arn>` — check in
      the console afterwards that SES is allowed to publish to the topic.)
- [x] Already in place — nothing to do: the domain identity is verified
      with Easy DKIM and signs its mail; a DMARC record is published; the
      **account-level suppression list** is on for bounces and complaints,
      so SES itself stops mailing an address that hard-bounced or
      complained.

## The request

SES console → **Account dashboard → Request production access** (or *View
Get set up page*). Field by field:

| Field | Enter |
|---|---|
| Mail type | **Transactional** |
| Website URL | `https://poc.elenchus.chat` |
| Additional contacts | the administrator's address(es) — where AWS sends its questions and its decision |
| Preferred contact language | English |
| Acknowledgement | tick it |

**Use case description** — paste this (about 2,700 characters, well inside
the field's limit). Replace the two bracketed items.

```text
Elenchus (https://poc.elenchus.chat; open source, documentation at
https://www.bradleypallen.org/elenchus-server/) is a research web
application run by [the University of Amsterdam / the Alliance for Data
Science and AI] for a study funded by the Alfred P. Sloan Foundation
(grant G-2026-79650). We use Amazon SES only for transactional account
email. We send no marketing, newsletters or bulk mail, and we do not buy,
rent or import address lists.

WHO RECEIVES MAIL. Only people who hold an account on the platform.
Accounts cannot be self-registered: an administrator creates each one by
issuing an invitation to a named individual — study staff at the partner
institutions and a panel of about five invited expert reviewers — who has
already agreed to take part. We expect fewer than 25 recipients in total.
Study participants are not emailed by the platform at all.

WHAT WE SEND. Four plain-text messages, each triggered by an action of
the person concerned or of an administrator acting for them: (1) an
account invitation with a single-use sign-up link; (2) a password-reset
link, only on request; (3) a one-time sign-in link, only on request; (4)
a notice that the account's password was changed. Example: "You have been
invited to Elenchus as a judge. Click here to create your account:
https://poc.elenchus.chat/?token=... If you weren't expecting this
invitation, ignore this email." Mail is sent from no-reply@elenchus.chat;
the domain is verified with Easy DKIM and publishes a DMARC record.

VOLUME. Typically fewer than 10 messages a week; at most about 30 in a
day when a review panel is invited. We are asking to leave the sandbox
not for volume but because we cannot ask each invited reviewer to verify
their address with AWS.

ABUSE PREVENTION. The public sign-in page can request a sign-in or reset
link, but the application sends one only to an active, registered account
and at most five times per fifteen minutes per account; requests for any
other address send nothing. Every other message requires an authenticated
administrator.

BOUNCES AND COMPLAINTS. The account-level suppression list is enabled for
both. Bounce and complaint notifications are published to an SNS topic
and delivered to the administrator, [name / role], who corrects the
address or deactivates the account. The application also reports any
message the mail server refuses to the administrator's dashboard at once.
At this volume every bounce is handled individually.

OPTING OUT. These are account messages rather than subscriptions.
A recipient can ask the study team to deactivate their account at any
time, after which the application sends them no further mail; nobody is
mailed before they have been individually invited.
```

The same thing by CLI, if you prefer (put the description in a file):

```bash
aws sesv2 put-account-details --region us-east-1 \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url https://poc.elenchus.chat \
  --contact-language EN \
  --additional-contact-email-addresses you@example.org \
  --use-case-description file://use-case.txt
```

AWS usually answers within a day, by email and in the Support Center case
it opens. While the case is open, `get-account` shows
`Details.ReviewDetails.Status` as `PENDING`.

## If they write back

They often do, with a templated request for "more detail". Answer in the
case, plainly. The questions they tend to ask, and the true answers:

- *How do you collect addresses?* — An administrator types in the address
  of a person who has agreed to take part. There is no sign-up form and no
  imported list.
- *How do you handle bounces and complaints?* — Suppression list on;
  notifications to a named administrator by SNS; each one handled by hand
  (fix the address or deactivate the account).
- *How can recipients unsubscribe?* — Transactional account mail only; an
  account is deactivated on request and then receives nothing.
- *How often do you mail one person?* — A handful of times over the life
  of the study: one invitation, and a reset or sign-in link when they ask.

## After it is granted

- [ ] Confirm: `aws sesv2 get-account --region us-east-1 --query
      ProductionAccessEnabled` → `true`.
- [ ] Issue an invitation to an address of yours that was **not** verified
      with SES. The Invites tab should say *also emailed*, the System tab's
      Email row should show the last message accepted — and it should
      arrive.
- [ ] Optionally have alerts emailed: set `ALERT_EMAIL_TO` in
      `/etc/elenchus/elenchus.env` and restart
      ([OPERATIONS.md](../docs/OPERATIONS.md)).
- [ ] Watch **SES → Reputation metrics** occasionally. At this volume one
      complaint is a large percentage; the suppression list and the
      invitation-only design are what keep it at zero.
- [ ] Keep telling admins to **hold on to the link** until the person
      confirms they are in ([Study Runbook](../docs/study-runbook.md)):
      accepted by a mail server is not the same as read.
