# --------------------------------------------------------------------------- #
# Where. Every value is set by .github/workflows/aws.yml from GitHub
# secrets and variables (TF_VAR_*). Credentials come from the workflow.
# --------------------------------------------------------------------------- #
variable "aws_region" {
  type        = string
  description = "Region to deploy the agent host into."
  default     = "us-east-1"
}

variable "availability_zone" {
  type        = string
  description = "Availability zone for the subnet and instance. Empty picks the first available zone."
  default     = ""
}

# --------------------------------------------------------------------------- #
# What to build
# --------------------------------------------------------------------------- #
variable "name" {
  type        = string
  description = "Name prefix for every resource. The secret is <name>/env and the instance role is <name>-ec2."
  default     = "jobspy-lite"

  validation {
    condition     = can(regex("^[A-Za-z0-9-]{1,50}$", var.name))
    error_message = "name must be 1-50 characters: letters, digits, and hyphens."
  }
}

variable "secret_recovery_window_days" {
  type        = number
  description = "Days Secrets Manager keeps the secret after destroy. 0 deletes it immediately."
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "instance_type" {
  type        = string
  description = "EC2 size. t3.small has enough RAM for the Docker build; t3.micro fits the free tier and relies on the 2GB swapfile."
  default     = "t3.small"
}

variable "ami_arch" {
  type        = string
  description = "Ubuntu image architecture. Use arm64 with Graviton types (t4g.*)."
  default     = "amd64"

  validation {
    condition     = contains(["amd64", "arm64"], var.ami_arch)
    error_message = "ami_arch must be amd64 or arm64."
  }
}

variable "ami_id" {
  type        = string
  description = "Pin an AMI. Empty resolves the latest Ubuntu 22.04 LTS from Canonical."
  default     = ""
}

variable "root_volume_gb" {
  type        = number
  description = "Root gp3 volume size. The image build and SQLite data both live here."
  default     = 30

  validation {
    condition     = var.root_volume_gb >= 16
    error_message = "root_volume_gb must be at least 16 so the image build fits."
  }
}

variable "ssh_public_key" {
  type        = string
  description = "OpenSSH public key authorized on the instance (ssh-ed25519 or ssh-rsa)."

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521) ", var.ssh_public_key))
    error_message = "ssh_public_key must be a single OpenSSH public key line."
  }
}

variable "ssh_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to SSH. The deploy job SSHes from GitHub-hosted runners, whose addresses change, so keep this open unless you use self-hosted runners."
  default     = ["0.0.0.0/0"]
}

variable "app_port" {
  type        = number
  description = "Host port published by docker compose. Must stay 8000 unless compose is changed too."
  default     = 8000

  validation {
    condition     = var.app_port > 0 && var.app_port < 65536
    error_message = "app_port must be a TCP port."
  }
}

variable "app_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to open the dashboard. Set JOBSPY_API_TOKEN in the Secrets Manager secret when this is the public internet."
  default     = ["0.0.0.0/0"]
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "subnet_cidr" {
  type    = string
  default = "10.0.1.0/24"
}

locals {
  # AMI names say amd64; the EC2 architecture filter says x86_64.
  ami_architecture = var.ami_arch == "amd64" ? "x86_64" : "arm64"
}
