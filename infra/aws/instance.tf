# --------------------------------------------------------------------------- #
# Compute: Ubuntu 22.04, booted with deploy/cloud-init-aws.yaml (Docker,
# swap, ~/app). An Elastic IP keeps the dashboard address stable across
# stop/start. AMI and user_data are ignored after launch so a later apply
# does not replace the box and wipe SQLite.
# --------------------------------------------------------------------------- #
data "aws_ami" "ubuntu" {
  count       = var.ami_id == "" ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-jammy-22.04-${var.ami_arch}-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = [local.ami_architecture]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

locals {
  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ami.ubuntu[0].id
}

resource "aws_key_pair" "app" {
  key_name   = var.name
  public_key = var.ssh_public_key
}

resource "aws_instance" "app" {
  ami                         = local.ami_id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.app.id]
  key_name                    = aws_key_pair.app.key_name
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.app.name
  monitoring                  = false
  user_data = templatefile("${path.module}/../../deploy/cloud-init-aws.yaml", {
    aws_region = var.aws_region
    secret_id  = aws_secretsmanager_secret.app.name
  })

  # The instance role policy must exist before a deploy can read the secret.
  depends_on = [aws_iam_role_policy.secrets]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  lifecycle {
    ignore_changes = [ami, user_data]
    precondition {
      condition     = local.ami_id != null && local.ami_id != ""
      error_message = "No Ubuntu AMI resolved. Set ami_id to an Ubuntu 22.04 AMI in this region."
    }
  }

  tags = {
    Name = var.name
  }
}

resource "aws_eip" "app" {
  domain = "vpc"

  # The address is only reachable once the internet gateway exists.
  depends_on = [aws_internet_gateway.igw]

  tags = {
    Name = var.name
  }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}
