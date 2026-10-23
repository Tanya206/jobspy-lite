#!/usr/bin/env bash
# Deploy JobSpy Lite onto the AWS instance from infra/aws.
#
# Usage (run on your LOCAL machine, from anywhere in the repo):
#   bash deploy/deploy-aws.sh                      # IP from terraform output
#   bash deploy/deploy-aws.sh ubuntu@<ELASTIC_IP>
#
# Optional:
#   AWS_SSH_KEY=~/.ssh/jobspy-aws   private key matching ssh_public_key
#
# What it does:
#   1. Packs the repo (excluding .venv, node_modules, data, .git, .env, terraform state).
#   2. Streams it over SSH into ~/app on the server.
#   3. Waits for cloud-init. The instance role reads Secrets Manager and writes ~/app/.env.
#   4. Builds and starts the stack with docker compose on the server.
#   5. Health-checks http://127.0.0.1:8000/health from the server.
#
# Put the secret before the first deploy (see infra/aws/README.md). Overrides:
#   JOBSPY_SECRET_ID    Secrets Manager name (default: terraform output secret_name)
#   JOBSPY_AWS_REGION   region (default: terraform output aws_region)
#
# Requires: tar, ssh, scp, and (for the no-argument form) terraform.
# On Windows run inside Git Bash or WSL.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f Dockerfile ]]; then
  echo "Run this from the jobspy-lite repo (Dockerfile not found at ${ROOT})." >&2
  exit 1
fi

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ConnectTimeout=10)
if [[ -n "${AWS_SSH_KEY:-}" ]]; then
  SSH_OPTS+=(-i "$AWS_SSH_KEY" -o IdentitiesOnly=yes)
fi

if [[ $# -ge 1 ]]; then
  TARGET="$1"
else
  if ! command -v terraform >/dev/null 2>&1; then
    echo "Usage: bash deploy/deploy-aws.sh ubuntu@<ELASTIC_IP>" >&2
    echo "terraform is not on PATH, so the Elastic IP could not be read." >&2
    exit 1
  fi
  IP="$(terraform -chdir="${ROOT}/infra/aws" output -raw public_ip 2>/dev/null || true)"
  if [[ -z "$IP" ]]; then
    echo "No public_ip in infra/aws state. Apply Terraform first, or pass ubuntu@<IP>." >&2
    exit 1
  fi
  TARGET="ubuntu@${IP}"
fi

REMOTE_DIR="~/app"
ARCHIVE="/tmp/jobspy-lite-aws.tgz"

tf_out() {
  if command -v terraform >/dev/null 2>&1; then
    terraform -chdir="${ROOT}/infra/aws" output -raw "$1" 2>/dev/null || true
  fi
}

AWS_REGION="${JOBSPY_AWS_REGION:-$(tf_out aws_region)}"
SECRET_ID="${JOBSPY_SECRET_ID:-$(tf_out secret_name)}"

remote() {
  ssh "${SSH_OPTS[@]}" "$TARGET" "$@"
}

echo "==> Packing repo (.env is not included — the host reads Secrets Manager)..."
tar czf "$ARCHIVE" \
  --exclude=.venv --exclude=node_modules --exclude=frontend/dist \
  --exclude=.git --exclude=data --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.env --exclude=secret.json \
  --exclude=.terraform --exclude=terraform.tfstate --exclude=terraform.tfstate.backup \
  --exclude=tfplan --exclude=plan.txt --exclude=backup \
  --exclude=terraform.tfvars \
  .

echo "==> Waiting for SSH on ${TARGET}..."
for i in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" -o LogLevel=ERROR "$TARGET" "true" 2>/dev/null; then
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    echo "SSH to ${TARGET} did not come up. Check the security group (ssh_cidrs) and the instance state." >&2
    exit 1
  fi
  sleep 10
done

echo "==> Uploading to ${TARGET}:${REMOTE_DIR}..."
remote "mkdir -p ${REMOTE_DIR}"
scp "${SSH_OPTS[@]}" "$ARCHIVE" "$TARGET:/tmp/jobspy-lite-aws.tgz"
rm -f "$ARCHIVE"

echo "==> Waiting for first-boot setup (cloud-init)..."
if ! remote "cloud-init status --wait"; then
  echo "cloud-init failed. Last log lines:" >&2
  remote "sudo tail -40 /var/log/cloud-init-output.log || true"
  exit 1
fi

if remote "docker info >/dev/null 2>&1"; then
  DOCKER_PREFIX=""
elif remote "sudo docker info >/dev/null 2>&1"; then
  DOCKER_PREFIX="sudo "
else
  echo "==> Docker missing — installing..."
  remote "sudo apt-get update -y \
    && sudo apt-get install -y docker.io \
    && (sudo apt-get install -y docker-compose-v2 || sudo apt-get install -y docker-compose) \
    && sudo systemctl enable --now docker \
    && sudo usermod -aG docker ubuntu"
  DOCKER_PREFIX="sudo "
fi

if remote "${DOCKER_PREFIX}docker compose version >/dev/null 2>&1"; then
  DC="${DOCKER_PREFIX}docker compose"
else
  DC="${DOCKER_PREFIX}docker-compose"
fi
echo "==> Using compose command: ${DC}"

echo "==> Unpacking on the server..."
remote "cd ${REMOTE_DIR} && tar xzf /tmp/jobspy-lite-aws.tgz && rm -f /tmp/jobspy-lite-aws.tgz"

if ! remote "command -v aws >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1"; then
  echo "==> Installing awscli and python3..."
  remote "sudo apt-get update -y && sudo apt-get install -y awscli python3"
fi

echo "==> Reading secrets from Secrets Manager (values stay on the host)..."
remote "AWS_REGION=$(printf '%q' "$AWS_REGION") SECRET_ID=$(printf '%q' "$SECRET_ID") bash ~/app/deploy/fetch-aws-env.sh ~/app/.env"

echo "==> Building + starting on the server (detached — safe on small instances)..."
remote "cd ${REMOTE_DIR} && nohup ${DC} up --build -d > build.log 2>&1 & echo build started"

echo "==> Polling the build (up to ~15 min) — Ctrl+C stops watching; the build continues remotely..."
HEALTH=""
for i in $(seq 1 60); do
  sleep 15
  HEALTH="$(remote "curl -s --max-time 3 http://127.0.0.1:8000/health 2>/dev/null || true")"
  if [[ -n "$HEALTH" ]]; then
    echo "==> Stack is up: $HEALTH"
    break
  fi
  FAILED="$(remote "cd ${REMOTE_DIR} && grep -m1 -E '(^|[^a-zA-Z])(ERROR|error:|failed to solve)' build.log 2>/dev/null || true")"
  if [[ -n "$FAILED" ]] && ! remote "pgrep -f 'compose.*up --build' >/dev/null"; then
    echo "==> BUILD FAILED: $FAILED" >&2
    remote "cd ${REMOTE_DIR} && tail -30 build.log"
    exit 1
  fi
  echo "    ...still building (${i}/60)"
done

HOST="${TARGET#*@}"
if [[ -z "$HEALTH" ]]; then
  echo "==> Timed out waiting; the build continues on the server. Check later with:"
  echo "    ssh ${TARGET} 'cd ~/app && tail -20 build.log && curl -s http://127.0.0.1:8000/health'"
  exit 0
fi

echo
echo "==> Done. Dashboard: http://${HOST}:8000"
echo "    If the browser cannot connect, confirm the security group allows TCP 8000"
echo "    (app_cidrs in infra/aws) and that cloud-init finished."
