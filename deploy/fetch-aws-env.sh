#!/usr/bin/env bash
# On the EC2 host: read the app secret with the instance role and write ~/app/.env.
# The secret value is never printed. Requires the AWS CLI and python3.
#
# Usage:
#   AWS_REGION=us-east-1 SECRET_ID=jobspy-lite/env bash deploy/fetch-aws-env.sh ~/app/.env
# If those vars are empty, /etc/jobspy/aws.env (written by cloud-init) is sourced.

set -euo pipefail

DEST="${1:-$HOME/app/.env}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ -z "${SECRET_ID:-}" || -z "${AWS_REGION:-}" ]]; then
  if [[ -f /etc/jobspy/aws.env ]]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/jobspy/aws.env
    set +a
  fi
fi

if [[ -z "${SECRET_ID:-}" || -z "${AWS_REGION:-}" ]]; then
  echo "SECRET_ID and AWS_REGION are required (or /etc/jobspy/aws.env)." >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is not installed on this host." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not installed on this host." >&2
  exit 1
fi

umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if ! aws secretsmanager get-secret-value \
  --region "$AWS_REGION" \
  --secret-id "$SECRET_ID" \
  --output json >"$tmp"; then
  echo "Could not read secret ${SECRET_ID} in ${AWS_REGION}." >&2
  echo "Set the AWS_APP_ENV GitHub secret and re-run the AWS workflow — see infra/aws/README.md." >&2
  exit 1
fi

python3 "${HERE}/render_aws_secret.py" "$tmp" "$DEST"
echo "Wrote ${DEST} from secret ${SECRET_ID} (values not shown)."
