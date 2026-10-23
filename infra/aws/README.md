# AWS (Terraform + deploy script)

A single EC2 host running the same Docker Compose stack as the Oracle Cloud
deployment. State lives in this directory and is independent of `infra/`
(the OCI stack).

| Layer | Tool | Entry point |
|---|---|---|
| VPC, subnet, security group, instance, Elastic IP | Terraform | `infra/aws/` |
| API keys and Telegram credentials | Secrets Manager | secret `<name>/env` |
| First-boot server setup (Docker, AWS CLI, swap, `~/app`) | cloud-init | `deploy/cloud-init-aws.yaml` |
| App build + restart | shell | `deploy/deploy-aws.sh` |

The instance runs Ubuntu 22.04. The app listens on port 8000. SQLite, the
resume, and the Excel export stay in `~/app/data` on the root volume.

## One-time setup

### 1. AWS credentials

```bash
aws configure            # or: aws sso login --profile your-profile
aws sts get-caller-identity
```

The IAM principal needs EC2 permissions for a VPC, subnet, internet gateway,
route table, security group, key pair, instance, and Elastic IP, plus
`ec2:DescribeImages`, `iam:PassRole`, and Secrets Manager
(`CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DeleteSecret`).

### 2. SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/jobspy-aws -C jobspy-lite
```

### 3. Variables

```bash
cd infra/aws
cp terraform.tfvars.example terraform.tfvars
```

Put the contents of `~/.ssh/jobspy-aws.pub` in `ssh_public_key`. Narrow
`ssh_cidrs` and `app_cidrs` to your public IP when you can. The dashboard
has no login of its own — set `JOBSPY_API_TOKEN` in the secret below when
port 8000 is open to the internet.

### 4. Apply

```bash
terraform init
terraform apply
terraform output dashboard_url
```

First boot takes a few minutes (Docker, AWS CLI, and a 2GB swapfile). Changing
`ami` or cloud-init later does not replace the instance. To rebuild the host
on purpose: `terraform apply -replace=aws_instance.app`. That deletes the root
disk, including `~/app/data`. Snapshot the volume first if you need the DB.
If the first apply fails while attaching the instance profile, run apply again
(IAM can take a few seconds to propagate).

### 5. Store the secrets

The instance role can read one secret and nothing else. Put a JSON object
whose keys match `.env.example`. Empty strings are dropped. A raw dotenv
body is also accepted.

```bash
aws secretsmanager put-secret-value \
  --region "$(terraform output -raw aws_region)" \
  --secret-id "$(terraform output -raw secret_name)" \
  --secret-string "{\"NVIDIA_API_KEY\":\"nvapi-...\",\"TELEGRAM_BOT_TOKEN\":\"123:abc\",\"TELEGRAM_CHAT_ID\":\"123456\",\"JOBSPY_API_TOKEN\":\"choose-a-long-token\"}"
```

Optional keys: `OPENAI_API_KEY`, `REWRITE_API_KEY`, `OTEL_EXPORTER_OTLP_HEADERS`.
This value is not stored in Terraform state. Updating it is another
`put-secret-value`; the next deploy rewrites `~/app/.env` from it.

## GitHub Actions

Two workflows, independent of the OCI jobs:

| Workflow | File | When |
|---|---|---|
| Terraform AWS | `.github/workflows/terraform-aws.yml` | plan on PRs/pushes to `infra/aws`; apply is **Run workflow** only |
| Deploy AWS | `.github/workflows/deploy-aws.yml` | push to `master` (app/frontend/Docker) or **Run workflow** |

Repo secrets:

| Secret | Used by | Value |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | Terraform AWS | IAM user/key that can manage the stack |
| `AWS_SECRET_ACCESS_KEY` | Terraform AWS | matching secret |
| `AWS_REGION` (variable) | Terraform AWS | optional; default `us-east-1` |
| `TF_VAR_AWS_SSH_PUBLIC_KEY` | Terraform AWS | contents of `~/.ssh/jobspy-aws.pub` |
| `AWS_HOST` | Deploy AWS | Elastic IP (`terraform output public_ip`) |
| `AWS_SSH_USER` | Deploy AWS | `ubuntu` |
| `AWS_SSH_KEY` | Deploy AWS | private key matching that public key |

After the first apply, set `AWS_HOST` from the job log (`public_ip`). Deploy
rsyncs the repo, rewrites `~/app/.env` from Secrets Manager on the instance,
then `docker compose up --build -d`. CI never uploads `.env`.

If you already applied this stack on your laptop, do not apply from Actions
until that state is in the workflow cache (or an S3 backend) — an empty CI
state would create a second VPC and instance.

## Deploy the app

From the repo root (Git Bash or WSL on Windows):

```bash
AWS_SSH_KEY=~/.ssh/jobspy-aws bash deploy/deploy-aws.sh
```

The script reads the Elastic IP and secret name from Terraform state. Pass
`ubuntu@<IP>` to skip the IP lookup. It does not upload a local `.env`. On
the host, `deploy/fetch-aws-env.sh` calls `secretsmanager:GetSecretValue`
with the instance role and writes `~/app/.env` as mode 600, then
`docker compose up --build -d` starts the stack.

Then open the dashboard URL, upload a resume, and start the scheduler.

## Day-to-day

```bash
bash deploy/deploy-aws.sh                         # ship code after a change
ssh ubuntu@<IP> 'cd ~/app && docker compose logs -f --tail 100'
ssh ubuntu@<IP> 'cd ~/app && tail -30 build.log'  # if the health check times out
```

A replaced instance keeps the Elastic IP and gets a new host key. Clear the
old one with `ssh-keygen -R <IP>` before the next SSH.

Datacenter IPs are often blocked by LinkedIn and Indeed. If a run scrapes
zero jobs, set proxies in `config.yaml` and deploy again. To rotate a key,
run `put-secret-value` and deploy again.

## What gets created

- VPC `10.0.0.0/16`, public subnet `10.0.1.0/24`, internet gateway
- security group: TCP 22 and TCP 8000 in, all egress
- Ubuntu 22.04 instance (`t3.small`, 30GB encrypted gp3) with an instance role
- Elastic IP
- Secrets Manager secret `jobspy-lite/env`
- first boot: Docker, compose plugin, AWS CLI, 2GB swap, `/home/ubuntu/app`

Rough monthly cost in us-east-1: the instance (~$15 for t3.small, or the
free-tier t3.micro), about $2.40 for 30GB gp3, $0.40 for one secret, and no
charge for an Elastic IP that stays attached. Stopped instances still pay for
the disk and the unattached-address rate if you disassociate the IP.

## Tear down

```bash
cd infra/aws
terraform destroy
```

Destroying the instance deletes the root volume and the database on it.
The secret is scheduled for deletion and its name stays reserved for
`secret_recovery_window_days` (7 by default). Set that variable to 0 before
destroy to delete the secret immediately.
