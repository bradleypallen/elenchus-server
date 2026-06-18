# Terraform + provider version pins for the Elenchus AWS PoC.
# PoC only — synthetic data, no participants. See README.md.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "elenchus"
      Component = "poc"
      ManagedBy = "terraform"
    }
  }
}

# Route 53 health-check CloudWatch metrics live only in us-east-1, so the
# optional uptime alarm is created through this aliased provider.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = {
      Project   = "elenchus"
      Component = "poc"
      ManagedBy = "terraform"
    }
  }
}
