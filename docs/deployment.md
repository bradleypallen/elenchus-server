# Deployment

How to run an Elenchus server, from a laptop to a production study host.
This page frames the options; the detailed procedures live in the
[Operations Runbook](OPERATIONS.md) and the [Cloud Deployment
plan](cloud-deployment.md).

## The one constraint that dictates everything

Each dialectic is a DuckDB file, and **DuckDB is single-writer-per-file**.
Elenchus therefore runs as exactly **one** server process over its data
directory. The consequences are non-negotiable:

- **No** `uvicorn --workers N`, no second container, no autoscaling group
  against the same data — concurrent writers corrupt the files.
- Scale **vertically** (a bigger box), not horizontally.
- The data directory must be a **local** disk, never a network filesystem
  (EFS/NFS/Filestore).
- Horizontal scale means migrating the platform DB to Postgres — the
  `db/registry.py` boundary is shaped for that swap, and it's the
  documented trigger, not a thing to improvise.

A single small VM (2 vCPU / 4 GB RAM) comfortably handles a pilot of a
couple dozen participants.

## Pick your path

| Situation | Path |
|---|---|
| Local development / trying it out | [Quick local](#quick-local) |
| Production study on one VM | [Operations Runbook](OPERATIONS.md) |
| Managed cloud (AWS / GCP), IaC, managed TLS & backups | [Cloud Deployment plan](cloud-deployment.md) |
| Throwaway proof-of-concept to validate the mechanics | [PoC scaffolds](#proof-of-concept) |

## Quick local

```bash
pip install "elenchus>=0.2.0"
export ELENCHUS_API_KEY=sk-ant-...
elenchus admin create --email admin@local --name "Admin"   # one-time
elenchus                                                    # serves on :8741
```

Open `http://localhost:8741`, log in, and you're running. This is fine
for development; it has no TLS, no backups, and no process supervision —
don't put participants on it.

## Production single VM

The [Operations Runbook](OPERATIONS.md) is the canonical procedure:
dedicated user + venv, the environment file, `systemd` unit (graceful
shutdown, raised file-descriptor limit, hardening), Nginx for TLS
termination and reverse proxy (with `X-Forwarded-Proto` so secure cookies
flow), `/healthz` monitoring, the backup cron, log rotation, per-study
export, and a pre-pilot checklist. Everything stateful lives in
`$ELENCHUS_DATA` on persistent, backed-up storage.

Key environment variables (full list in the [User Guide](guide.md) and
runbook):

| Variable | Purpose |
|---|---|
| `ELENCHUS_API_KEY` | LLM API key (required) |
| `ELENCHUS_DATA` | data directory (platform DB, bases, backups, exports) |
| `ELENCHUS_MODEL` | model (default `claude-opus-4-6`) |
| `SESSION_COOKIE_SECURE` | `true` behind HTTPS |
| `BCRYPT_ROUNDS` | password cost (12 in production) |
| `ELENCHUS_SECRET_KEY` | master key to encrypt the admin-set API key at rest (so a UI-set key survives restarts) |
| `ELENCHUS_ENABLE_PHASE_B` | **leave unset** for the Sloan study |

## Managed cloud

The [Cloud Deployment plan](cloud-deployment.md) keeps the same
single-instance core and wraps it with managed TLS, object-store backups,
secrets, monitoring, and Terraform. It's cloud-agnostic with concrete
AWS and Google Cloud mappings, a SURF (UvA) option, DNS/domain notes for
`elenchus.chat`, cost estimates, and a proof-of-concept-first posture
gated on DPO sign-off before any real launch.

## Proof of concept

For a disposable run that validates the mechanics with **synthetic data
only** (no participants, no DPO gate), two scaffolds live in the repo:

- [`deploy/manual-poc.md`](https://github.com/bradleypallen/elenchus-server/blob/main/deploy/manual-poc.md)
  — one box, by hand: a single VM + `pip install` + Nginx + `certbot
  --nginx`, ~30 minutes, no IaC. The on-box steps match a SURF VM, so
  nothing is wasted. *(This exact path was executed and verified
  end-to-end before being torn down.)*
- [`deploy/aws-poc/`](https://github.com/bradleypallen/elenchus-server/tree/main/deploy/aws-poc)
  — a reproducible, `terraform destroy`-able single-EC2 scaffold
  (encrypted EBS, `certbot dns-route53`, `EXPORT`→S3, SSM, auto-recovery,
  secrets read from SSM at boot) for when you want repeatable infra.

Tear PoC infrastructure down when you're done — it bills while it runs.
