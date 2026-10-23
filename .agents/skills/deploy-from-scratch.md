---
name: deploy-from-scratch
description: >
  Step-by-step runbook to deploy JobSpy Lite from zero to a fresh cloud
  environment (OCI via Terraform + GitHub Actions, or any Ubuntu VM via the
  portable SSH deploy script), including every secret, environment variable,
  and verification step. Use whenever the user asks to deploy, re-deploy to a
  new server/account, or set up the project in a fresh environment.
---

# Deploy JobSpy Lite from scratch

Three supported paths:

- **Path A — OCI + Terraform + GitHub Actions** (full IaC; what production uses)
- **Path B — Any Ubuntu VM + deploy script** (fastest; works anywhere Docker runs)
- **Path C — AWS + Terraform + deploy script** (`infra/aws/`, then `deploy/deploy-aws.sh`)

Prerequisites common to both: this repo cloned, `docker` + `git` + `ssh` on your
machine, an NVIDIA API key, and a Telegram bot token + chat id.

---

## Secrets and environment variables (complete list)

### App secrets — `.env` on the server (gitignored, NEVER committed)

Create `.env` in the repo root (copy `.env.example`):

| Key | Required | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | yes | LLM key for scoring (integrate.api.nvidia.com) |
| `OPENAI_API_KEY` | when `ai.provider` is openai | OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | numeric id (NOT the t.me link). Get it: message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `OTEL_EXPORTER_OTLP_HEADERS` | no | trace export auth; the endpoint is `tracing.otel_endpoint` in config.yaml |

Scrape, screening, workers, rate limit, rescore window, proxies, and the scheduler live in `config.yaml`, not `.env`.

### CI/CD secrets — GitHub repo → Settings → Secrets and variables → Actions

| Secret | Used by | Value |
|---|---|---|
| `OCI_HOST` | Deploy app | server public IP |
| `OCI_SSH_USER` | Deploy app | `ubuntu` |
| `OCI_SSH_KEY` | Deploy app | private key PEM matching the instance's authorized key |
| `TF_VAR_TENANCY_OCID` | Terraform | tenancy OCID |
| `TF_VAR_USER_OCID` | Terraform | IAM user OCID |
| `TF_VAR_FINGERPRINT` | Terraform | API key fingerprint |
| `TF_VAR_PRIVATE_KEY` | Terraform | API signing key PEM body |
| `TF_VAR_REGION` | Terraform | e.g. `ap-hyderabad-1` |
| `TF_VAR_SSH_PUBLIC_KEY` | Terraform | public key authorized on the instance |
| `TF_VAR_IMAGE_OCID` | Terraform | pinned Ubuntu image OCID (region-specific; see step A5) |
| `AWS_ACCESS_KEY_ID` | Terraform AWS | IAM access key for the AWS stack |
| `AWS_SECRET_ACCESS_KEY` | Terraform AWS | matching secret key |
| `AWS_REGION` (Actions variable) | Terraform AWS | e.g. `us-east-1` (optional; workflow default) |
| `TF_VAR_AWS_SSH_PUBLIC_KEY` | Terraform AWS | contents of `~/.ssh/jobspy-aws.pub` |
| `AWS_HOST` | Deploy AWS | Elastic IP from `terraform output public_ip` |
| `AWS_SSH_USER` | Deploy AWS | `ubuntu` |
| `AWS_SSH_KEY` | Deploy AWS | private key matching `TF_VAR_AWS_SSH_PUBLIC_KEY` |

Notes learned the hard way: exact casing of `TF_VAR_*` doesn't matter in GitHub
(workflow maps them), but a trailing newline or a mangled PEM breaks things —
prefer setting secrets via GitHub API from the source files, not copy-paste.

---

## Path A — OCI, Terraform + GitHub Actions (from absolute zero)

### A1. OCI API key
Console → Profile icon → User settings → API keys → **Add API key** → Generate →
**download the PEM**. Note the fingerprint shown, plus tenancy OCID and user
OCID on the same page. Region: e.g. `ap-hyderabad-1`.

### A2. Create the GitHub repo and push
```bash
git remote add origin https://github.com/<user>/jobspy-lite.git
git push -u origin master        # CI runs on push; it needs no secrets
```

### A3. Add all CI secrets from the table above
Fastest reliable way (uses the stored git credential to seal secrets):
```bash
# helper: set-secret NAME VALUE_FILE
```
Or the UI. `TF_VAR_*` values come from A1; `OCI_HOST` is filled in A6 (skip for
now); `OCI_SSH_KEY` = contents of your local private key file.

### A4. Free-tier limits to know
- VCNs: **2 per region** — a failed apply can orphan one and block you (delete
  orphans before retrying)
- Instances: 2× E2.1.Micro (AMD) or 4 OCPU/24GB A1.Flex (ARM)
- A failed apply's state must be preserved (the workflow does this via
  run-scoped cache saves; don't reintroduce exact-key cache restores)

### A5. Pin the image OCID
The `oci_core_images` data source can return null in some regions. Get a valid
Ubuntu OCID: Console → Compute → Custom Images/Images, pick e.g.
`Canonical-Ubuntu-22.04-*` (or 20.04), copy its OCID → set `TF_VAR_IMAGE_OCID`.
Works cross-shape.

### A6. Apply Terraform
Actions → **Terraform infra** → Run workflow (apply is manual-gated).
Watch the run; on success the job log prints:
```
public_ip = "x.x.x.x"
```
Update the `OCI_HOST` secret to that IP.

### A7. First boot (≈3–5 min)
cloud-init installs Docker + compose plugin, adds a 2GB swapfile, opens ports
8000/80 in iptables, puts `ubuntu` in the docker group. Wait for:
```bash
ssh ubuntu@IP "cloud-init status"        # → status: done
ssh ubuntu@IP "docker ps"                # → works without sudo
```
(If `docker ps` says permission denied, re-login — group membership is
login-time.)

### A8. Ship the app secrets
```bash
ssh ubuntu@IP "mkdir -p ~/app"
scp .env ubuntu@IP:~/app/.env
```

### A9. Deploy the app
Actions → **Deploy app** → Run workflow. Green = the app is live.

### A10. Verify
```bash
curl http://IP:8000/health
# {"status":"ok","ai_model":...,"resume_uploaded":false,...}
```
Then: open `http://IP:8000` → upload resume → start the scheduler → set
interval (30–60 min recommended; 5 min stacks runs) → force one run.

---

## Path B — Any Ubuntu VM with the deploy script

1. Provision any Ubuntu 20.04+ VM with your SSH key; note its IP.
2. `ssh ubuntu@IP` — install Docker if missing:
   `sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker ubuntu` (re-login after).
3. From the repo root:
   ```bash
   bash deploy/deploy-oci.sh ubuntu@IP
   ```
   Packs the repo (includes `.env` — create it first!), uploads to `~/app`,
   self-heals Docker, runs a detached build, polls `/health` up to ~15 min.
4. Verify: `curl http://IP:8000/health` → dashboard → resume → scheduler.

Troubleshooting: `docker compose logs -f --tail 100` on the server;
`tail -50 ~/app/build.log` for build failures; check cloud/provider firewall
(security list + OS iptables) before suspecting the app.

---

## Path C — AWS, Terraform + deploy script

State and variables live in `infra/aws/` (separate from the OCI stack in `infra/`).
Full write-up: `infra/aws/README.md`.

1. `aws sts get-caller-identity` works (CLI profile or env credentials).
2. `ssh-keygen -t ed25519 -f ~/.ssh/jobspy-aws -C jobspy-lite`
3. `cd infra/aws && cp terraform.tfvars.example terraform.tfvars` and paste
   `~/.ssh/jobspy-aws.pub` into `ssh_public_key`. Narrow `ssh_cidrs` /
   `app_cidrs` to your IP when you can. Set `JOBSPY_API_TOKEN` inside the
   Secrets Manager JSON if port 8000 is open to the internet.
4. `terraform init && terraform apply`. Note `terraform output dashboard_url`.
5. Store the app secrets (they are not read from a local `.env` on deploy):
   `aws secretsmanager put-secret-value --region $(terraform output -raw aws_region) --secret-id $(terraform output -raw secret_name) --secret-string '{"NVIDIA_API_KEY":"...","TELEGRAM_BOT_TOKEN":"...","TELEGRAM_CHAT_ID":"..."}'`
6. GitHub secrets for the AWS workflows: `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, `TF_VAR_AWS_SSH_PUBLIC_KEY`, then after apply
   `AWS_HOST` (the Elastic IP), `AWS_SSH_USER=ubuntu`, `AWS_SSH_KEY`.
   Actions → **Terraform AWS** → Run workflow (apply is manual). Then
   Actions → **Deploy AWS** → Run workflow. App keys stay in Secrets
   Manager; CI never ships `.env`.
7. From the repo root the script still works:
   `AWS_SSH_KEY=~/.ssh/jobspy-aws bash deploy/deploy-aws.sh`
   The instance role fetches the secret and writes `~/app/.env` (mode 600).
8. `curl http://IP:8000/health`, then upload a resume and start the scheduler.

Replacing the instance (`terraform apply -replace=aws_instance.app`) deletes
the root disk, including `~/app/data`. The Elastic IP stays; clear the old
host key with `ssh-keygen -R <IP>`. Tear down with `terraform destroy` in
`infra/aws/`.

---

## Post-deploy checklist

- [ ] `/health` returns `status: ok` from outside the server
- [ ] Resume uploaded (`resume_uploaded: true`)
- [ ] Scheduler on, interval ≥ 30 min, active hours 8–22
- [ ] Telegram: one message arrives for a force-run above threshold
- [ ] `docker compose ps` shows healthy; `docker compose logs` clean
- [ ] Repo CI green; a trivial commit auto-deploys (push-to-master path)

## Rollback / teardown

- App: previous code ships again by reverting the commit and pushing.
- Infra: Actions → Terraform infra → Run workflow after adding a `destroy` step,
  or console-delete instance then VCN children then VCN. State cache must be
  cleared afterwards.
