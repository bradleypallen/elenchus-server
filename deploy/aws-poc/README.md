# Elenchus AWS PoC (single instance)

A self-contained, `terraform destroy`-able proof-of-concept that stands up
one Elenchus server on AWS to **get the deployment mechanics right** — on
the *substrate-portable* path (single EC2 + Nginx + `certbot dns-route53`
+ `EXPORT`→S3), so most of what you learn carries to SURF later. See the
plan in [`../../docs/cloud-deployment.md`](../../docs/cloud-deployment.md).

> **PoC only — synthetic data, no participants, no DPO gate.** A
> synthetic-only PoC processes no personal data, so standing it up needs
> no data-steward / DPO sign-off. Drive it with `elenchus sim`,
> `scripts/run_dialectic.py`, and seeded demo accounts — **never real
> participants**. The DPO gate applies to the production launch (likely
> SURF). Tear this down when you're done evaluating.

## What it creates

A minimal VPC (one public subnet), **one** EC2 instance (Ubuntu 24.04,
single-writer — never an ASG), a separate **encrypted EBS data volume**
mounted at `/var/lib/elenchus`, an Elastic IP, a Route 53 `A` record for
`hostname`, a private **S3 bucket** for `EXPORT DATABASE` backups, a
least-privilege instance role, a status-check **auto-recovery** alarm,
and (optionally) a `/healthz` uptime alarm by email. TLS is obtained on
the box via `certbot dns-route53` using the instance role. Admin shell is
**SSM Session Manager** — no SSH port is open.

## Prerequisites

- Terraform ≥ 1.6, AWS CLI, credentials for an account where you can
  create VPC/EC2/IAM/S3/Route 53/SSM resources.
- `elenchus.chat` registered in Route 53 **in this account** (the config
  looks the hosted zone up by name).
- Network egress to GitHub from the instance. `elenchus_package` defaults
  to a `git+https://…@main` spec because **PyPI's `elenchus` is stale
  (0.1.1, pre multi-user platform)** — installing the plain `elenchus`
  wheel gives a server with no auth/admin. Once 0.2.0+ ships to PyPI you
  can override `elenchus_package` back to a pinned PyPI version.

## Use

```bash
cd deploy/aws-poc
cp terraform.tfvars.example terraform.tfvars   # edit le_email etc.

# 1. Put the secrets in SSM (NOT in tfvars/state). region must match.
aws ssm put-parameter --region eu-central-1 --type SecureString --overwrite \
  --name /elenchus/poc/anthropic_api_key --value "sk-ant-..."
aws ssm put-parameter --region eu-central-1 --type SecureString --overwrite \
  --name /elenchus/poc/admin_password   --value "$(openssl rand -base64 24)"

# 2. Stand it up.
terraform init
terraform plan          # review first — this is unvalidated against a live account
terraform apply

# 3. Wait ~3-5 min for cloud-init, then verify.
curl -sf "$(terraform output -raw healthz)" | jq
#  expect: {"status":"ok", "phase_b_enabled":false, "llm_configured":true, ...}

# Shell in (no SSH):
eval "$(terraform output -raw ssm_connect)"
#  on the box: sudo tail -f /var/log/elenchus-bootstrap.log

# 4. Exercise it with SYNTHETIC data only (from your workstation/CI):
#    point the sim/recorder at the public URL, or run them in-process.

# 5. Tear it down when done.
terraform destroy
```

## Notes / gotchas

- **Single-writer is enforced by shape:** one `aws_instance`, no scaling
  group. Don't add one — it corrupts DuckDB. (See the constraint section
  in the plan doc.)
- **Secrets never touch Terraform state** — they're read from SSM at boot
  by the instance role. Config (hostname, email, model, bucket) *is* in
  state; that's fine.
- **`SSM_PREFIX` coupling:** `user_data.sh` hard-codes `/elenchus/poc`;
  it must match `var.ssm_prefix`. Change both together if you ever do.
- **First boot reformats the data volume; re-boots do not** — the script
  only `mkfs`-es a disk with no filesystem, and mounts an existing one
  by UUID, so an instance replacement preserves the (synthetic) data.
- **DNS/cert propagation** can take a few minutes on first apply; if
  `certbot` runs before the Route 53 record resolves it still works
  (it's a DNS-01 challenge against the zone, not HTTP).
- **What transfers to SURF:** the systemd unit, the data-volume mount,
  Nginx + `certbot dns-route53` (works off-AWS using the Route 53 zone),
  the `EXPORT`→object-store backup, `/healthz`. What does **not**: the
  VPC/EIP/EBS/IAM/SSM/CloudWatch wiring (SURF has its own equivalents).
- **Cost:** roughly a few dollars a day while running (t3.small + EBS +
  EIP + a little S3/Route 53); `terraform destroy` stops it.
- **Not yet validated against a live AWS account** — `terraform plan`
  first, and treat the first `apply` as the real test of these files.
