# Elenchus AWS PoC — single EC2 instance on the substrate-portable path.
#
# SINGLE-WRITER CONSTRAINT: DuckDB is single-writer-per-file, so this is
# ONE aws_instance — never an Auto Scaling Group, never >1. Do not
# "scale this out"; that corrupts the data files. See docs/cloud-deployment.md.
#
# PoC ONLY: synthetic data, no participants, no DPO gate. terraform destroy
# when done.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# Latest Ubuntu 24.04 LTS (Python 3.12; apt has nginx + certbot + the
# route53 plugin). insecure_value because an AMI id is not a secret.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_route53_zone" "this" {
  name         = "${var.root_zone_name}."
  private_zone = false
}

locals {
  az          = data.aws_availability_zones.available.names[0]
  bucket_name = "elenchus-poc-backups-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# ── Network (minimal public-subnet VPC; cheap, self-contained) ──────────

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "elenchus-poc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "elenchus-poc" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = "elenchus-poc-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "elenchus-poc-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Inbound 80/443 only (Nginx). No SSH — admin via SSM Session Manager.
# The app port 8741 is never exposed; Nginx proxies to it on localhost.
resource "aws_security_group" "web" {
  name        = "elenchus-poc-web"
  description = "Elenchus PoC: HTTP/HTTPS in, all out"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  ingress {
    description = "HTTP (redirect to HTTPS / ACME http fallback)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "elenchus-poc-web" }
}

# ── Data volume (separate, encrypted; mounted at /var/lib/elenchus) ─────

resource "aws_ebs_volume" "data" {
  availability_zone = local.az
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "elenchus-poc-data" }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf" # appears as an NVMe device on Nitro; user_data detects it
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.this.id
}

# ── Stable address + DNS ────────────────────────────────────────────────

resource "aws_eip" "this" {
  domain   = "vpc"
  instance = aws_instance.this.id
  tags     = { Name = "elenchus-poc" }
}

resource "aws_route53_record" "a" {
  zone_id = data.aws_route53_zone.this.zone_id
  name    = var.hostname
  type    = "A"
  ttl     = 60
  records = [aws_eip.this.public_ip]
}

# ── Backups bucket (versioned, private, lifecycle-expired) ──────────────

resource "aws_s3_bucket" "backups" {
  bucket = local.bucket_name
  tags   = { Name = "elenchus-poc-backups" }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "expire-poc-backups"
    status = "Enabled"
    filter {}
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 14 }
  }
}

# ── SSM config (non-secret; the static bootstrap reads these) ───────────
# Secrets are NOT created here — set them out-of-band so they never land
# in Terraform state (see README): ${var.ssm_prefix}/anthropic_api_key and
# ${var.ssm_prefix}/admin_password as SecureString.

resource "aws_ssm_parameter" "hostname" {
  name  = "${var.ssm_prefix}/hostname"
  type  = "String"
  value = var.hostname
}
resource "aws_ssm_parameter" "le_email" {
  name  = "${var.ssm_prefix}/le_email"
  type  = "String"
  value = var.le_email
}
resource "aws_ssm_parameter" "model" {
  name  = "${var.ssm_prefix}/model"
  type  = "String"
  value = var.model
}
resource "aws_ssm_parameter" "s3_bucket" {
  name  = "${var.ssm_prefix}/s3_bucket"
  type  = "String"
  value = local.bucket_name
}
resource "aws_ssm_parameter" "package" {
  name  = "${var.ssm_prefix}/package"
  type  = "String"
  value = var.elenchus_package
}

# ── Instance identity (least-privilege) ─────────────────────────────────

resource "aws_iam_role" "instance" {
  name = "elenchus-poc-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Keyless shell via Session Manager (no SSH) + CloudWatch agent.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy_attachment" "cw" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "app" {
  name = "elenchus-poc-app"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadConfigAndSecrets"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}/*"
      },
      {
        Sid      = "DecryptSecureStringsViaSSM"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com" }
        }
      },
      {
        Sid      = "Certbot-Route53-ZoneScoped"
        Effect   = "Allow"
        Action   = ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"]
        Resource = "arn:aws:route53:::hostedzone/${data.aws_route53_zone.this.zone_id}"
      },
      {
        Sid      = "Certbot-Route53-Global"
        Effect   = "Allow"
        Action   = ["route53:ListHostedZones", "route53:GetChange"]
        Resource = "*"
      },
      {
        Sid      = "BackupBucketList"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.backups.arn
      },
      {
        Sid      = "BackupBucketObjects"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.backups.arn}/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "elenchus-poc-instance"
  role = aws_iam_role.instance.name
}

# ── The one instance ────────────────────────────────────────────────────

resource "aws_instance" "this" {
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  user_data                   = file("${path.module}/user_data.sh")
  user_data_replace_on_change = true

  root_block_device {
    encrypted   = true
    volume_size = 16
    volume_type = "gp3"
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  tags = { Name = "elenchus-poc" }

  # Config params must exist before the instance boots and reads them.
  depends_on = [
    aws_ssm_parameter.hostname,
    aws_ssm_parameter.le_email,
    aws_ssm_parameter.model,
    aws_ssm_parameter.s3_bucket,
    aws_ssm_parameter.package,
  ]
}

# ── Monitoring ──────────────────────────────────────────────────────────

# Self-heal on a failed system status check (same instance, same volume).
resource "aws_cloudwatch_metric_alarm" "autorecover" {
  alarm_name          = "elenchus-poc-autorecover"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  alarm_actions       = ["arn:aws:automate:${data.aws_region.current.name}:ec2:recover"]
  dimensions          = { InstanceId = aws_instance.this.id }
}

# Optional /healthz uptime check → email. Route 53 health-check metrics
# only exist in us-east-1, so the topic + alarm are created there.
resource "aws_sns_topic" "alerts" {
  count    = var.alert_email != "" ? 1 : 0
  provider = aws.us_east_1
  name     = "elenchus-poc-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_route53_health_check" "healthz" {
  count             = var.alert_email != "" ? 1 : 0
  fqdn              = var.hostname
  type              = "HTTPS"
  port              = 443
  resource_path     = "/healthz"
  request_interval  = 30
  failure_threshold = 3
  tags              = { Name = "elenchus-poc-healthz" }
}

resource "aws_cloudwatch_metric_alarm" "healthz" {
  count               = var.alert_email != "" ? 1 : 0
  provider            = aws.us_east_1
  alarm_name          = "elenchus-poc-healthz"
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
  dimensions          = { HealthCheckId = aws_route53_health_check.healthz[0].id }
}
