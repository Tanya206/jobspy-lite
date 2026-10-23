output "public_ip" {
  description = "Elastic IP of the agent host."
  value       = aws_eip.app.public_ip
  depends_on  = [aws_eip_association.app]
}

output "instance_id" {
  description = "EC2 instance id."
  value       = aws_instance.app.id
}

output "availability_zone" {
  value = aws_instance.app.availability_zone
}

output "ssh_command" {
  description = "Shell access. Re-run ssh-keygen -R on this IP after a replace."
  value       = "ssh ubuntu@${aws_eip.app.public_ip}"
}

output "dashboard_url" {
  description = "JobSpy Lite dashboard."
  value       = "http://${aws_eip.app.public_ip}:${var.app_port}"
}

output "aws_region" {
  description = "Region the secret and instance live in."
  value       = var.aws_region
}

output "secret_name" {
  description = "Secrets Manager name. Put the JSON environment here before deploying."
  value       = aws_secretsmanager_secret.app.name
}

output "secret_arn" {
  description = "Secrets Manager ARN the instance role can read."
  value       = aws_secretsmanager_secret.app.arn
}
