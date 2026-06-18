# Cloud deployment plan — DuckDB pilot (single-instance)

This plans a managed cloud deployment of an Elenchus server for the Sloan
pilot, on the **DuckDB / single-instance** approach (decision: stay on
DuckDB for the pilot; migrate to Postgres only when a concrete scale/HA
trigger fires — see "Scaling trigger"). It is **cloud-agnostic first**,
then mapped to **SURF** (the national research-IT route, and the likely
first choice for a UvA pilot), **AWS**, and **Google Cloud**.

It *extends* [`OPERATIONS.md`](OPERATIONS.md) — the systemd unit, Nginx
config, `scripts/backup.py`, `/healthz`, and the pre-pilot checklist
there are the on-instance core; this document wraps managed cloud
services and ops machinery around that core. Don't duplicate them.

## The one constraint that dictates everything

**DuckDB is single-writer-per-file → the app runs as exactly one
process.** Therefore:

- **One VM, one process.** No autoscaling group / managed instance group
  with size > 1; no container autoscaling; no serverless. (A fixed-size-1
  group is acceptable *only* as a self-healing replacer, and only if you
  guarantee the data volume is attached read-write to one instance at a
  time — single-instance auto-recovery is simpler and safer.)
- **Never put `$ELENCHUS_DATA` on a network filesystem** (EFS / Filestore
  / NFS) — DuckDB's file locking is unsafe there. Use block storage.
- **Vertical scale only.** Horizontal scale = the Postgres migration,
  which the `db/registry.py` boundary is shaped to absorb. Out of scope
  for the pilot.

Pilot scale is tiny (≤24 participants); a small VM is ample. The work
here is durability, security, and hands-off ops — not throughput.

## Cloud-agnostic reference architecture

```
            DNS (study hostname)
                  │
        ┌─────────▼──────────┐     managed, auto-renewing TLS cert
        │  L7 HTTPS load      │◀──── (terminates TLS; /healthz health
        │  balancer           │      check; access logs; WAF-ready)
        └─────────┬──────────┘
                  │ HTTP :8741  (X-Forwarded-Proto: https)
        ┌─────────▼──────────────────────────────┐
        │  ONE VM  ── systemd ── elenchus (1 proc)│
        │   ├ reads secrets at boot (no secrets   │
        │   │  on disk) via VM identity            │
        │   ├ ships logs+metrics to monitoring     │
        │   └ admin access via keyless session     │
        │      broker (no open SSH)                │
        │            │                              │
        │   ┌────────▼─────────┐ persistent block  │
        │   │ $ELENCHUS_DATA   │ volume (encrypted,│
        │   │  platform.duckdb │ separate from OS  │
        │   │  bases/…         │ disk)             │
        │   │  backups/ exports/                    │
        │   └────────┬─────────┘                    │
        └────────────┼──────────────────────────────┘
       scheduled     │              app EXPORT-DATABASE
       block         │              archives + per-study
       snapshots ────┘              exports ──► object store
                                    (versioned; exports bucket
                                     locked; pseudonym map kept
                                     OUT of the archive)
   egress ──► LLM API · SMTP (institutional relay) · object store
```

Building blocks (provider-neutral):

| # | Block | Purpose |
|---|-------|---------|
| a | One always-on VM, systemd-managed single process | the app (single-writer) |
| b | Managed L7 HTTPS LB + managed auto-renew cert | hands-off TLS, health check, stable hostname |
| c | DNS record → LB | the study URL |
| d | Separate encrypted block volume for `$ELENCHUS_DATA` | data survives OS rebuilds |
| e | Scheduled block snapshots (managed lifecycle) | fast full-volume restore |
| f | App `EXPORT DATABASE` archives → object store (versioned) | logical, portable, engine-version-independent backup |
| g | Locked object-store location for per-study exports | research data; pseudonym map stored separately |
| h | Secrets manager (API key, SMTP, admin/backup creds) | no secrets on disk; fetched via VM identity |
| i | Email via institutional SMTP relay (preferred) or cloud email | invites, magic links, alerts |
| j | Central logs + metrics + alarms; uptime check on `/healthz` | observability + paging |
| k | Keyless, audited shell (no inbound SSH) | admin access |
| l | Least-privilege VM identity | read its secrets, write its prefixes, ship telemetry — nothing else |
| m | Terraform + baked image / cloud-init | reproducible infra + provisioning |

## Non-obvious decisions (so nobody "optimizes" it into corruption)

- **Single process — enforced, not incidental.** The biggest failure mode
  is a well-meaning move to "scale it out." Document it at the IaC level
  (instance count hard-pinned to 1).
- **Managed auto-renew certs require a managed LB** — you can't install
  them on the VM directly. That's why the LB is in the design even for
  one instance. Budget alternative: VM-terminated TLS with Let's Encrypt
  (`OPERATIONS.md` Nginx+certbot path), trading the LB cost for manual
  cert ops.
- **LB timeout ≥ 120 s.** LLM turns run up to ~30 s; default 60 s LB
  idle/backend timeouts will truncate them. Also set connection draining
  / deregistration delay so in-flight turns + the WAL flush finish on the
  graceful SIGTERM shutdown.
- **Forward-only migrations + single-writer ⇒ deploy = snapshot → update
  → verify → (restore to roll back).** No blue/green (can't run two
  writers on one volume); deploy stops the old process and starts the new
  one on the same instance+volume → brief downtime. Schedule deploys
  outside session windows. Verify on `/healthz` (it returns
  `schema_version`, `phase_b_enabled` — must be `false` — and
  `llm_configured` — must be `true`).
- **EU region (GDPR).** UvA = Netherlands; keep data in the EU, sign the
  provider DPA, and keep the per-study **pseudonym map out of the export
  archive and out of any deposit-eligible bucket** (the app already
  writes it separately).

## DNS & domain

The study domain is **`elenchus.chat`**, registered via **Route 53**.
Registrar/DNS are decoupled from compute — owning the domain on Route 53
does **not** commit the app to AWS hosting; a Route 53 record can point
at a VM on any substrate (including SURF).

- **Hosted zone:** `elenchus.chat` in Route 53 (created with the
  registration); manage all records there.
- **Hostname:** decide apex (`elenchus.chat`) vs subdomain
  (`app.`/`study.elenchus.chat`). A static-IP VM takes an A record at the
  apex; if fronted by an ALB, use a Route 53 **alias** at the apex (apex
  can't be a CNAME).
- **TLS by substrate:**
  - *AWS + ALB:* ACM cert, DNS-validated automatically via the Route 53
    zone, auto-renewed — no manual cert ops.
  - *SURF / any plain VM:* `certbot` with the **`dns-route53`** plugin —
    Let's Encrypt DNS-01 against the Route 53 zone (an IAM principal
    scoped to that zone), unattended issue + renew, supports apex +
    wildcard. Hands-off certs off-AWS using the zone you already own.
- **Email sender ≠ web domain:** send participant mail from the UvA
  `@uva.nl` SMTP relay (deliverability/trust) even though links point to
  `elenchus.chat`; frame it in the invite so the mismatch doesn't read as
  phishing.
- **Registration hygiene:** auto-renew ON (a lapse takes the site down),
  transfer-lock, and track which AWS account owns it (continuity; the DPO
  will care if that account also hosts).

## Ops machinery ("managing such a deployment")

- **IaC — Terraform** (one config, modules: `network`, `compute+volume`,
  `lb+cert+dns`, `secrets`, `storage`, `observability`, `iam`). Pin
  instance count = 1. Tear-down-able, reviewable.
- **Image & provisioning** — bake a golden image (Packer: OS + Python +
  the package + the monitoring agent) so instance replacement is
  deterministic; minimal cloud-init does mount-data-volume → fetch-secrets
  → start service. Alternative: pure cloud-init from a stock image.
- **Deploy/update** — release artifact (tagged build → object store or a
  package registry) → **snapshot** → update + `systemctl restart` via the
  session broker's run-command → **verify `/healthz`** → roll back by
  restore if needed. Migrations auto-apply at startup.
- **Backups — two layers + a drill.**
  - *Block snapshots* (managed schedule, e.g. every 6–24 h, 14-day
    retention) — fast whole-volume restore.
  - *App `EXPORT DATABASE` archives* (`scripts/backup.py`, the existing
    cron) synced to the object store — logical, portable across DuckDB
    versions; this is the authoritative restore for engine upgrades.
  - **Restore drill on a schedule** — an untested backup is not a backup.
    Periodically restore the latest archive into a throwaway instance and
    run `elenchus audit` + `/healthz`.
  - RPO ≈ snapshot/export interval (tunable; sessions are resumable, so a
    few hours' loss is recoverable). RTO ≈ minutes (attach volume / launch
    from snapshot + boot).
- **Monitoring/alerting** — uptime check on `/healthz` (assert 200 +
  `phase_b_enabled:false`, `llm_configured:true`); data-volume disk-usage
  > 80 %; file-descriptor count (registry warns < 256); instance status →
  auto-recover; error-rate via a log metric filter on `ERROR` /
  `LLMCallError` categories; **cost budget** alarm. Route the app's own
  `ALERT_EMAIL_TO` alerts through the same email path so platform alerts
  and infra alerts land together.
- **Access** — keyless session-broker shell only; **no inbound SSH**;
  audit logging on; OS patching via the provider's patch manager.
- **DR / recovery runbook** — instance failure → auto-recover (same
  volume, same data) or launch a new instance + attach the existing data
  volume + restore-if-needed; document the exact steps and the RTO/RPO.
- **Scaling trigger** — vertical resize only. Migrate to Postgres (and
  then a stateless multi-instance tier) when *any* of: need >1 app
  instance / HA SLA, multi-region, concurrent load beyond one VM, or the
  platform graduates from pilot to standing shared service.

## Security & compliance

- Encryption at rest (block volume + object store, provider KMS/CMEK) and
  in transit (TLS to the LB and from app → SMTP/LLM/object store).
- Least-privilege VM identity; **no static credentials** anywhere.
- App port reachable only from the LB; admin only via the session broker;
  egress allowed for LLM API + email + object store.
- Audit logging (CloudTrail / Cloud Audit Logs) on; consider VPC flow
  logs.
- **GDPR:** EU region + signed DPA; run a DPIA if UvA requires; document
  retention + erasure; exports bucket access-restricted; pseudonym map
  stored apart from the archive and excluded from any public deposit.

## Cost (rough monthly, pilot scale)

VM (small) + L7 LB + block volume (20–50 GB) + object store + DNS +
logs/metrics + secrets ≈ **~$50–80/mo**, plus LLM spend (~$15–25 per full
pilot run, tracked in-app via the usage table). The LB is the largest
fixed line item; the Nginx+certbot budget path removes it.

---

## Hosting at UvA (SURF) — the likely first choice

For a University of Amsterdam pilot, the national research-IT route is
usually preferable to a personal hyperscaler account: NL/EU-resident,
pre-vetted for sensitive research data, institutional login, and
*subsidised* for Dutch researchers (a compute allocation, not a
credit-card bill) — which makes the DPO/IRB conversation far easier.
Lead with this; fall back to AWS/GCP only if SURF lacks something
specific.

**SURF** (the Dutch national education/research ICT cooperative; UvA is a
member) gives you VMs you fully control, so the single-instance design
above applies almost unchanged — SURF is just the substrate. You trade
the hyperscalers' *managed* TLS/secrets/monitoring for the on-VM
equivalents `OPERATIONS.md` already documents, and gain residency,
compliance, institutional login, and (likely) no direct bill.

| Block | SURF / UvA option |
|-------|-------------------|
| VM | **SURF Research Cloud** workspace (a VM; IaaS, choose OS+software; built for sensitive data) — or **SURF HPC Cloud** (self-managed, straightforwardly persistent) |
| Data volume | workspace/VM block storage; **Research Drive** for adjacent research data |
| TLS / DNS | **ICTS** for the `*.uva.nl` subdomain + records; terminate TLS on the VM (Nginx + cert) — no managed-LB-with-auto-cert like the hyperscalers, so the `OPERATIONS.md` Nginx+certbot path is the natural fit |
| Backups | volume snapshots (per SURF) + the app `EXPORT DATABASE` archives → Research Drive / object storage; **run the restore drill** |
| Secrets | no hyperscaler-style secrets manager — root-owned `0640` env file on the VM (per `OPERATIONS.md`) or a self-hosted store |
| Email | UvA institutional **SMTP relay** (recommended regardless) |
| Logs/metrics | on-VM (journald + log rotation) + SURF monitoring where available; a simple external `/healthz` uptime check |
| Identity/access | institutional login (**SURFconext**); SSH per SURF's access model |
| Region | NL / EU by default |

**Authoritative gate.** *Where participant data may be hosted* is decided
by your faculty **data steward**, **RDM Support** (`rdm-support@uva.nl`),
and the **privacy officer (FG/DPO)** — not by technical fit. Engage them
first; they also set the DMP/DPIA requirements. Request the SURF
allocation via your institution's access route / SURF **Cloud Research
Consultancy**, and the `uva.nl` subdomain via **ICTS**.

**Two things to confirm explicitly:**

- **Persistence.** SURF Research Cloud workspaces carry a budget/"wallet"
  and can be oriented to interactive / time-bounded use — confirm you can
  run a **24/7, long-lived** workspace for the pilot's duration, or use
  **HPC Cloud** (persistent by default). The single-writer constraint
  holds either way.
- **LLM egress (a separate DPO item, independent of where the server
  sits).** Dialogue turns go to Anthropic's US API. In this study that
  content is *domain reasoning*; participant PII (identities/emails)
  stays in the platform DB and is not sent to the model — but the DPO
  must still bless the cross-border data flow and the processing terms.
  Often the longer pole than hosting.

## AWS mapping

| Block | AWS service | Notes |
|-------|-------------|-------|
| VM | **EC2** t3.small/medium (or t4g/Graviton for cost) | one instance, pinned |
| Data volume | **EBS gp3**, separate, KMS-encrypted | not the root volume |
| Snapshots | **Data Lifecycle Manager (DLM)** | schedule + retention |
| LB + cert | **ALB + ACM** (free auto-renew cert) | idle timeout 120 s; target-group health check → `/healthz`; deregistration delay ~30 s |
| DNS | **Route 53** | alias → ALB |
| Object store | **S3** | versioning; lifecycle → IA/Glacier; Block Public Access; separate bucket/prefix for exports |
| Secrets | **Secrets Manager** (or **SSM Parameter Store** SecureString, cheaper) | IAM-scoped |
| Email | **SES** (verify domain + DKIM, exit sandbox) — or institutional SMTP | prefer UvA relay |
| Logs/metrics | **CloudWatch Agent + Logs + Alarms**; **Synthetics canary** or Route 53 health check on `/healthz`; **EC2 auto-recovery** alarm | |
| Shell | **SSM Session Manager** (+ Patch Manager) | no SSH port |
| Identity | **IAM instance role** | read its secrets, write its S3 prefixes, CloudWatch, SSM |
| IaC / image | **Terraform** AWS provider; **Packer** AMI | |
| Region | **eu-central-1** (Frankfurt) / eu-west-1 / eu-north-1 | no NL region; Frankfurt is closest |

AWS gotchas: ACM public certs attach only to ALB/CloudFront, not EC2 —
that's the reason for the ALB. SSM + S3 work via a free S3 **gateway VPC
endpoint**; if you put the instance in a private subnet you need a **NAT
gateway** (~$32/mo) or interface endpoints for SSM/SES — for a pilot, a
public subnet with a tight security group (app port from the ALB SG only,
no SSH) + SSM is the cost-pragmatic posture. AWS has no live migration —
rely on auto-recovery for host failure and heed scheduled-retirement
events.

## Google Cloud mapping

| Block | GCP service | Notes |
|-------|-------------|-------|
| VM | **Compute Engine** e2-small/medium | sustained-use discount auto-applies; one instance |
| Data volume | **Persistent Disk** (balanced/SSD), separate, CMEK-encrypted | |
| Snapshots | **PD snapshot schedule** (resource policy) | |
| LB + cert | **External HTTPS Load Balancer + Google-managed cert** | backend timeout ≥ 120 s; health check → `/healthz` |
| DNS | **Cloud DNS** | |
| Object store | **Cloud Storage** | versioning; lifecycle → Nearline/Coldline; uniform bucket-level access + public-access prevention; separate bucket for exports |
| Secrets | **Secret Manager** | |
| Email | **no native service** → institutional SMTP relay (preferred) or third-party (SendGrid/Mailgun) | |
| Logs/metrics | **Ops Agent + Cloud Logging/Monitoring**; **uptime checks** → alerting policy | |
| Shell | **IAP TCP tunnel for SSH + OS Login** (IAM-tied); **VM Manager** for patching | no public IP / open SSH |
| Identity | **service account** attached to the VM (least-priv) | |
| IaC / image | **Terraform** Google provider; **Packer** image / instance template | |
| Region | **europe-west4 (Netherlands)** / europe-west1 | in-NL is a mild data-residency plus for UvA |

GCP notes: the managed cert needs the HTTPS LB (same reason as ACM/ALB).
**Live migration** transparently moves the VM during host maintenance —
a genuine availability plus for a single instance (planned host events
don't cause downtime); failures still need auto-restart. If you use a
managed instance group of size 1 for self-healing, note a read-write PD
attaches to only one VM (which *helps* enforce single-writer), but the
detach/reattach on replacement is fiddly — prefer a single instance with
auto-restart + live migration. No native transactional email is the only
real gap; the institutional SMTP relay closes it.

## Proof-of-concept first; launch gated on DPO sign-off

Plan of record: stand up a **synthetic-data-only PoC on AWS** to get the
mechanics right, and **do not launch with participants** until the SURF
discussion with the faculty data steward / privacy officer (FG-DPO) is
settled. The domain is already on Route 53, so AWS is the fast path to a
working PoC.

- **GDPR scope.** A synthetic-only PoC processes **no personal data** —
  drive it with `elenchus sim`, `scripts/run_dialectic.py`, and seeded
  demo accounts, never real participants — so standing it up needs no DPO
  gate. The data-steward / DPO gate applies to the **production launch
  with real participants**, wherever that lands (likely SURF).
- **What transfers.** The **portable core** carries to any substrate:
  provision a VM, attach + mount an encrypted data volume, run the one
  process under systemd, TLS, DNS, the two-layer backup + a restore
  drill, `/healthz` monitoring, deploy/restart, teardown. The
  **AWS-managed periphery** (ALB/ACM, Secrets Manager, DLM, SSM,
  CloudWatch) does *not* transfer 1:1 — SURF uses the on-VM equivalents
  in `OPERATIONS.md`.
- **Maximise transfer.** Since production is likely SURF (on-VM
  Nginx+certbot), the highest-value PoC exercises the **substrate-portable
  path**: single EC2 + Nginx + `certbot dns-route53` + `EXPORT`→S3 +
  `/healthz`. Add the ALB+ACM+Secrets-Manager managed variant only to
  evaluate the AWS-native option on its merits.
- **Cheap + disposable.** Everything in Terraform, `terraform destroy`-
  able; tear it down between sessions.

## Implementation roadmap

- **P0 — decisions:** provider (per institutional DPA/billing/credits),
  EU region (NL vs Frankfurt), instance size, study hostname, LB vs
  Nginx+certbot, email (UvA SMTP vs cloud). Open the account/Org + DPA.
- **P1 — core infra (Terraform):** network, VM, encrypted data volume,
  VM identity, secrets; cloud-init/Packer to install + mount + start;
  `/healthz` answering over HTTP on the instance.
- **P2 — TLS/DNS:** LB + managed cert + DNS; `SESSION_COOKIE_SECURE=true`;
  verify HTTPS end-to-end and `X-Forwarded-Proto`; LB timeout 120 s.
- **P3 — data safety:** snapshot schedule + `backup.py` EXPORT→object-store
  sync; admin bootstrap (`elenchus admin create`); `migrate-legacy` if
  importing; **run a restore drill**.
- **P4 — observability:** agent + log shipping + alarms + `/healthz`
  uptime check + cost budget; route `ALERT_EMAIL_TO` through the email
  path.
- **P5 — hardening + go-live gate:** tight SG/firewall, session-broker
  only, audit logs, encryption verified, GDPR/DPIA review; then run the
  `OPERATIONS.md` **pre-pilot checklist** (`elenchus sim`,
  `RUN_UI_E2E=1 pytest tests/e2e/`, healthz flags, invite-email smoke,
  alert smoke, backup verified, TLS valid).
- **P6 — go-live:** cut over DNS, monitor first sessions; deploy/restore/
  recover runbooks in hand.

## Open decisions

- Provider: **SURF first** for a UvA pilot (residency + subsidy + DPO-
  friendliness); AWS/GCP only as a fallback. The data steward / DPO and
  any existing institutional agreement are the real deciders.
- Region: in-NL (GCP europe-west4) vs Frankfurt (AWS eu-central-1).
- TLS: managed LB+cert (hands-off, ~LB cost) vs Nginx+certbot (cheaper,
  manual renew).
- Email: UvA SMTP relay (recommended for a university study) vs SES/3rd-party.
- Network posture: public subnet + tight SG + session broker (pilot-cheap)
  vs private subnet + NAT/endpoints (stricter).
- Self-healing: single-instance auto-recovery (recommended) vs size-1
  group.
