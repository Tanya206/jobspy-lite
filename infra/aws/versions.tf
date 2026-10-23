# Terraform + AWS provider pins.
# State stays in this directory (gitignored). For shared use, create an S3
# bucket first, then uncomment the backend below — the bucket cannot be
# created by this same configuration.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # backend "s3" {
  #   bucket = "jobspy-lite-tfstate"
  #   key    = "aws/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      project = "jobspy-lite"
      managed = "terraform"
    }
  }
}
