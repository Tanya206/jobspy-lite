# --------------------------------------------------------------------------- #
# Secrets Manager holds the app environment (API keys, Telegram, optional
# JOBSPY_API_TOKEN). Terraform creates the secret and lets only this instance
# read it. The secret value is set out of band, so it never lands in state
# or in the deploy archive.
# --------------------------------------------------------------------------- #
resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/env"
  description             = "JobSpy Lite environment variables (JSON object or dotenv)."
  recovery_window_in_days = var.secret_recovery_window_days

  tags = {
    Name = "${var.name}/env"
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = {
    Name = "${var.name}-ec2"
  }
}

resource "aws_iam_role_policy" "secrets" {
  name = "read-app-secret"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadAppEnv"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = aws_secretsmanager_secret.app.arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.name}-ec2"
  role = aws_iam_role.app.name

  tags = {
    Name = "${var.name}-ec2"
  }
}
