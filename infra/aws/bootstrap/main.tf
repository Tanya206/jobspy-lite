# --------------------------------------------------------------------------- #
# S3 bucket that holds Terraform state for infra/aws (and for this stack).
# The bootstrap job in .github/workflows/aws.yml applies this with local state
# the first time, then migrates that state into the bucket it just created.
# Locking uses S3 lock files (use_lockfile), so no DynamoDB table is needed.
# --------------------------------------------------------------------------- #
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

variable "aws_region" {
  type = string
}

variable "bucket_name" {
  type        = string
  description = "Globally unique; the workflow uses <name>-tfstate-<account>-<region>."
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

resource "aws_s3_bucket" "state" {
  bucket = var.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

output "bucket" {
  value = aws_s3_bucket.state.bucket
}
