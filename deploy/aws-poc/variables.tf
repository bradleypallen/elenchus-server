variable "region" {
  description = "AWS region (keep it in the EU for GDPR)."
  type        = string
  default     = "eu-central-1" # Frankfurt
}

variable "root_zone_name" {
  description = "The Route 53 hosted-zone name you registered (no trailing dot)."
  type        = string
  default     = "elenchus.chat"
}

variable "hostname" {
  description = "FQDN the PoC serves on. A subdomain so it doesn't squat the apex you may want for production."
  type        = string
  default     = "poc.elenchus.chat"
}

variable "le_email" {
  description = "Contact email for the Let's Encrypt registration (expiry notices)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type. t3.small is ample for the synthetic PoC."
  type        = string
  default     = "t3.small"
}

variable "data_volume_gb" {
  description = "Size of the separate EBS data volume mounted at /var/lib/elenchus."
  type        = number
  default     = 20
}

variable "model" {
  description = "ELENCHUS_MODEL for the PoC (a cheaper model is fine for synthetic runs)."
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach 80/443. Default open; restrict to your IPs for a private PoC."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "alert_email" {
  description = "If set, creates an SNS topic + a Route 53 /healthz uptime alarm that emails here. Empty = skip."
  type        = string
  default     = ""
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix for PoC config + secrets."
  type        = string
  default     = "/elenchus/poc"
}

variable "elenchus_package" {
  description = "What the instance pip-installs (PyPI spec, or a VCS/URL spec for a specific build)."
  type        = string
  default     = "elenchus"
}
