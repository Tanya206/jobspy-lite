# Terraform + AWS provider pins.
# Runs only in GitHub Actions (.github/workflows/aws.yml). State lives in the
# S3 bucket created by infra/aws/bootstrap; the workflow passes bucket, key,
# and region with -backend-config.

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project = "jobspy-lite"
      managed = "terraform"
    }
  }
}
