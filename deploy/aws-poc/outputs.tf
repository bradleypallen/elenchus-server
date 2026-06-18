output "url" {
  description = "The PoC URL (once DNS + cert have propagated)."
  value       = "https://${var.hostname}/"
}

output "healthz" {
  value = "https://${var.hostname}/healthz"
}

output "public_ip" {
  description = "Elastic IP of the instance."
  value       = aws_eip.this.public_ip
}

output "instance_id" {
  value = aws_instance.this.id
}

output "ssm_connect" {
  description = "Open a keyless shell (no SSH)."
  value       = "aws ssm start-session --target ${aws_instance.this.id} --region ${var.region}"
}

output "backups_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "set_secrets_hint" {
  description = "Run these BEFORE apply (or before the instance boots) so bootstrap can read them."
  value       = <<-EOT
    aws ssm put-parameter --region ${var.region} --type SecureString --overwrite \
      --name ${var.ssm_prefix}/anthropic_api_key --value "sk-ant-..."
    aws ssm put-parameter --region ${var.region} --type SecureString --overwrite \
      --name ${var.ssm_prefix}/admin_password   --value "<choose-a-strong-password>"
  EOT
}
