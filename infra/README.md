# Infrastructure as Code (Terraform + GitHub Actions)

The whole stack — OCI networking, instance, and the app deployment — is now
reproducible from the repo. AWS is a separate stack in [`infra/aws/`](aws/README.md)
with its own state. Three layers:

| Layer | Tool | Entry point |
|---|---|---|
| Cloud resources (VCN, subnet, security list, instance) | Terraform | `infra/` |
| First-boot server setup (Docker, swap, iptables) | cloud-init | `deploy/cloud-init.yaml` (auto-applied by Terraform) |
| App build + restart on the server | GitHub Actions | `.github/workflows/deploy-app.yml` |

## One-time setup

### 1. Create an OCI API key
Console → Profile icon → **User settings → API keys → Generate API key**.
You get a fingerprint and download a PEM. Note the tenancy OCID and user OCID
shown on that page.

### 2. GitHub repo secrets
Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `TF_VAR_tenancy_ocid` | tenancy OCID |
| `TF_VAR_user_ocid` | user OCID |
| `TF_VAR_fingerprint` | API key fingerprint |
| `TF_VAR_private_key` | full PEM body (with BEGIN/END lines) |
| `TF_VAR_region` | e.g. `us-ashburn-1` |
| `TF_VAR_ssh_public_key` | contents of `~/.ssh/oci_key.pub` |
| `OCI_HOST` | instance public IP (from `terraform output public_ip`) |
| `OCI_SSH_USER` | `ubuntu` |
| `OCI_SSH_KEY` | private key (PEM) matching that public key |

`TF_VAR_*` secrets are picked up by Terraform automatically — no tfvars needed
in CI. For local runs use `infra/terraform.tfvars` instead (copy
`terraform.tfvars.example`, gitignored).

## Day-to-day

```bash
# Infra changes (network/instance) — plan on PR, auto-apply on master:
git push                                    # CI runs tests, then plan/apply

# App changes — rebuilt on the server by CI on push to master:
git push                                    # or: gh workflow run deploy-app.yml

# From your machine, the classic way still works:
bash deploy/deploy-oci.sh ubuntu@<IP>
```

## What gets created

- `jobspy-vcn` (10.0.0.0/16) + internet gateway + route table
- public subnet `10.0.1.0/24`
- security list: ingress 22 (SSH) + 8000 (app), ICMP, all egress
- instance `jobspy-lite` (default `VM.Standard.E2.1.Micro`; switch to
  `VM.Standard.A1.Flex` in tfvars when capacity allows)
- first boot: cloud-init installs Docker + compose plugin, adds a 2GB swapfile,
  opens ports 8000/80 in the OS firewall

State lives in `infra/terraform.tfstate` (gitignored). For team use, move it
to an OCI Object Storage backend (S3-compatible) — see `infra/versions.tf`.

## First deploy of a brand-new instance

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # fill in your values
terraform init
terraform apply
terraform output public_ip                     # → set OCI_HOST secret
ssh ubuntu@<IP> 'mkdir -p ~/app'               # then push to master, or:
bash deploy/deploy-oci.sh ubuntu@<IP>
```

Then upload your resume at `http://<IP>:8000` and start the scheduler.
Secrets never pass through git or CI — `.env` stays only on the server.
