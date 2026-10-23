# AWS (Terraform in GitHub Actions)

A single EC2 host running the Docker Compose stack. Everything — state bucket,
infrastructure, app secrets, and deploys — runs through one workflow,
[`.github/workflows/aws.yml`](../../.github/workflows/aws.yml). All
configuration lives in the GitHub repo settings. There is no local
`terraform apply` or deploy script for AWS.

| Layer | Where it is defined | Applied by |
|---|---|---|
| Terraform state bucket (S3, versioned, encrypted) | `infra/aws/bootstrap/` | `bootstrap` job |
| VPC, subnet, security group, instance, Elastic IP, IAM role, secret | `infra/aws/` | `apply` job |
| App environment (API keys, Telegram, `JOBSPY_API_TOKEN`) | `AWS_APP_ENV` GitHub secret → Secrets Manager `<name>/env` | `deploy` job |
| First-boot setup (Docker, AWS CLI, swap, `~/app`) | `deploy/cloud-init-aws.yaml` | cloud-init |
| App build + restart | `deploy` job (rsync + `docker compose up --build`) | `deploy` job |

## What triggers what

| Event | Jobs |
|---|---|
| Pull request touching `infra/aws/**` or the workflow | `plan` (comment on the PR) |
| Push to `master` touching infra, app, frontend, Docker, or `config.yaml` | `bootstrap` → `apply` → `deploy` |
| Actions → **AWS** → Run workflow, `apply` | `bootstrap` → `apply` → `deploy` |
| Actions → **AWS** → Run workflow, `plan` | `bootstrap` → `plan` |
| Actions → **AWS** → Run workflow, `destroy` (type `jobspy-lite` to confirm) | `bootstrap` → `destroy` |

`apply`, `deploy`, and `destroy` run in the `aws` GitHub environment. Add a
required reviewer there (Settings → Environments → aws) if you want a manual
gate before changes reach AWS.

## GitHub configuration

Settings → Secrets and variables → Actions.

### Secrets

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Access key for an IAM user that can manage the stack (permissions below) |
| `AWS_SECRET_ACCESS_KEY` | Matching secret key |
| `TF_VAR_AWS_SSH_PUBLIC_KEY` | OpenSSH public key authorized on the instance (`ssh-ed25519 AAAA...`) |
| `AWS_SSH_KEY` | Private key matching that public key, with BEGIN/END lines |
| `AWS_APP_ENV` | JSON object of app environment variables (below) |

Generate the SSH pair once with `ssh-keygen -t ed25519 -N "" -f jobspy-aws -C jobspy-lite`,
paste both halves into the secrets, then delete the files.

`AWS_APP_ENV` uses the keys from `.env.example`. Empty strings are dropped.

```json
{
  "NVIDIA_API_KEY": "nvapi-...",
  "TELEGRAM_BOT_TOKEN": "123:abc",
  "TELEGRAM_CHAT_ID": "123456",
  "JOBSPY_API_TOKEN": "choose-a-long-token"
}
```

Optional keys: `OPENAI_API_KEY`, `REWRITE_API_KEY`, `OTEL_EXPORTER_OTLP_HEADERS`.
The deploy job writes it to Secrets Manager only when it differs from the
stored value; it never enters Terraform state. The instance role then writes
`~/app/.env` (mode 600) on the host. To rotate a key, edit the secret and
re-run the workflow. Set `JOBSPY_API_TOKEN` whenever port 8000 is open to the
internet — the dashboard has no login of its own.

### Variables (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Region for the state bucket and the stack |
| `AWS_INSTANCE_TYPE` | `t3.small` | `t3.micro` fits the free tier; `t4g.*` needs `AWS_AMI_ARCH=arm64` |
| `AWS_AMI_ARCH` | `amd64` | `amd64` or `arm64` |
| `AWS_ROOT_VOLUME_GB` | `30` | Root gp3 size (holds the image build and SQLite) |
| `AWS_APP_CIDRS` | `["0.0.0.0/0"]` | JSON list of CIDRs allowed to reach port 8000 |

SSH (port 22) stays open to `0.0.0.0/0` because the deploy job connects from
GitHub-hosted runners, whose addresses change. Login is key-only.

Changing `AWS_REGION` after the first apply points the workflow at a new,
empty state bucket and builds a second stack. Destroy first, then switch.

### IAM permissions for the access key

The key needs, in the chosen region:

- EC2: VPC, subnet, internet gateway, route table, security group, key pair,
  instance, Elastic IP, and `ec2:Describe*`
- IAM: create/delete/pass the `jobspy-lite-ec2` role and instance profile
  (`iam:CreateRole`, `iam:PutRolePolicy`, `iam:CreateInstanceProfile`,
  `iam:AddRoleToInstanceProfile`, `iam:PassRole`, and the matching
  Get/List/Delete/Remove actions)
- Secrets Manager on `jobspy-lite/env*`: Create, Describe, Get, Put, Delete,
  TagResource
- S3 on `jobspy-lite-tfstate-*`: CreateBucket, the bucket configuration
  Get/Put actions (versioning, encryption, public access block, lifecycle,
  tagging), ListBucket, GetObject, PutObject, DeleteObject
- `sts:GetCallerIdentity`

## First run

1. Add the five secrets above (and any variables).
2. Actions → **AWS** → Run workflow → `apply`.
3. `bootstrap` creates `jobspy-lite-tfstate-<account>-<region>` and moves its
   own state into it. `apply` builds the stack. `deploy` waits for cloud-init,
   syncs `AWS_APP_ENV`, uploads the repo, and builds the containers.
4. The `deploy` job prints the dashboard URL. Open it, upload a resume, and
   start the scheduler.

If the first apply fails while attaching the instance profile, re-run the
workflow (IAM can take a few seconds to propagate).

If an earlier version of this workflow applied the stack with state in the
GitHub Actions cache, the `apply` job pushes that cached state into S3 the
first time, so the existing instance is kept rather than duplicated.

## Day-to-day

- App or `config.yaml` change: push to `master`. `apply` is a no-op and
  `deploy` ships the code.
- Infrastructure change: open a PR to see the plan, then merge.
- Logs: the `deploy` job prints `build.log` on failure. For live logs, SSH in
  with the key from `AWS_SSH_KEY`: `ssh ubuntu@<IP> 'cd ~/app && docker compose logs -f --tail 100'`.

Datacenter IPs are often blocked by LinkedIn and Indeed. If a run scrapes zero
jobs, set proxies in `config.yaml` and push.

Changing the AMI or cloud-init does not replace the instance (`ignore_changes`),
because replacing it deletes the root disk and `~/app/data`. To rebuild the host
on purpose, destroy and apply again — snapshot the volume first if you need
the database.

## What gets created

- S3 bucket `jobspy-lite-tfstate-<account>-<region>` (versioned, encrypted,
  public access blocked, old state versions expire after 90 days)
- VPC `10.0.0.0/16`, public subnet `10.0.1.0/24`, internet gateway
- Security group: TCP 22 and TCP 8000 in, all egress
- Ubuntu 22.04 instance (`t3.small`, 30GB encrypted gp3) with an instance role
  that can read only the app secret
- Elastic IP
- Secrets Manager secret `jobspy-lite/env`

Rough monthly cost in us-east-1: the instance (~$15 for t3.small, or the
free-tier t3.micro), about $2.40 for 30GB gp3, $0.40 for the secret, a few
cents for the state bucket, and no charge for an attached Elastic IP.

## Tear down

Actions → **AWS** → Run workflow → `destroy`, and type `jobspy-lite` in
`confirm_destroy`.

This deletes the instance and its root volume, including the database. The
secret is scheduled for deletion and its name stays reserved for 7 days, so
an `apply` inside that window fails until the secret is restored or the
window passes. The state bucket is kept (`prevent_destroy`); delete it in the
S3 console if you are done with AWS entirely.
