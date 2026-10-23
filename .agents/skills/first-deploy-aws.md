---
name: first-deploy-aws
description: >
  Walk a first-time deployer through getting JobSpy Lite running on AWS via
  the GitHub Actions "AWS" workflow: AI provider key, Telegram bot and chat id,
  AWS IAM user, SSH key pair, every GitHub secret and variable, config.yaml
  review, the first workflow run, and post-deploy verification. Use when the
  user is deploying for the first time, setting up a fresh AWS account or
  fork, or asks which secrets or Telegram settings are needed.
---

# First deployment on AWS (GitHub Actions only)

AWS is deployed **only** through `.github/workflows/aws.yml`. Terraform runs
inside GitHub Actions with state in S3; every setting lives in the GitHub repo
(Settings → Secrets and variables → Actions). Reference: `infra/aws/README.md`.

## Rules for the agent

- Never run `terraform`, `aws secretsmanager put-secret-value`, or any deploy
  script locally for AWS. If something needs changing, change GitHub
  config or the repo and re-run the workflow.
- Never write secret values into the repo, a committed file, a PR, or chat
  output. Temporary files holding secrets (SSH keys, `app-env.json`) go
  outside the repo and are deleted once stored in GitHub.
- Ask the user for values only they can produce (API keys, Telegram token,
  AWS keys). Do the mechanical work (JSON assembly, `gh secret set`) for them.
- Work through the checklist in order and confirm each step before the next.

## Checklist

| # | Step | Produces |
|---|---|---|
| 1 | AI provider key | `OPENAI_API_KEY` or `NVIDIA_API_KEY` |
| 2 | Telegram bot + chat id | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| 3 | Dashboard token | `JOBSPY_API_TOKEN` |
| 4 | Assemble the app environment | `AWS_APP_ENV` secret |
| 5 | AWS IAM user + access key | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| 6 | SSH key pair | `TF_VAR_AWS_SSH_PUBLIC_KEY`, `AWS_SSH_KEY` |
| 7 | GitHub secrets, variables, `aws` environment | repo configured |
| 8 | Review `config.yaml` | search terms, location, timezone |
| 9 | Run the workflow | live instance + dashboard URL |
| 10 | Verify and finish setup in the dashboard | resume, scheduler, first alert |

---

## 1. AI provider key

`config.yaml` → `ai.provider` decides which key is required:

| `ai.provider` | Required key | Where to get it |
|---|---|---|
| `openai` (current repo default) | `OPENAI_API_KEY` | platform.openai.com → API keys. Billing must be enabled. |
| `nvidia` | `NVIDIA_API_KEY` (`nvapi-...`) | build.nvidia.com → any model → Get API Key |

`REWRITE_API_KEY` is optional; set it only when `ai.rewrite.base_url` points at
a different provider than scoring.

## 2. Telegram

The app sends one batched message per run listing every new job at or above
`screening.score_threshold`. Without these two values it still runs and just
logs a warning.

### 2a. Create the bot → `TELEGRAM_BOT_TOKEN`

1. In Telegram, open **@BotFather** (blue check mark) and send `/newbot`.
2. Give it a display name, then a username ending in `bot` (e.g. `myjobspy_bot`).
3. BotFather replies with a token like `123456789:AAH...`. That is
   `TELEGRAM_BOT_TOKEN`. Treat it as a password; `/revoke` in BotFather
   rotates it.

### 2b. Find the chat id → `TELEGRAM_CHAT_ID`

A bot cannot message someone who has not messaged it first.

**Personal alerts (most common):**

1. Open your bot (link from BotFather) and press **Start** or send `hi`.
2. Open in a browser (replace the token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789, ...}`. That number is `TELEGRAM_CHAT_ID`.

**Group alerts:** add the bot to the group, send `/start@<botusername>` in the
group, then read `getUpdates`. Group ids are negative (e.g. `-1001234567890`);
keep the minus sign.

**Channel alerts:** add the bot as a channel admin with "Post messages", post
once in the channel, and read `getUpdates`. Channel ids start with `-100`.

The id is a number, not the `t.me/...` link or `@username`.

If `getUpdates` returns `"result":[]`: send another message to the bot and
reload. If it still is empty, a webhook may be set; clear it with
`https://api.telegram.org/bot<TOKEN>/deleteWebhook` and retry.

### 2c. Test before deploying

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> -d text="JobSpy Lite test"
```

`"ok":true` and a message in Telegram means both values are right.
`chat not found` means the chat id is wrong or you never messaged the bot.
`Unauthorized` means the token is wrong.

## 3. Dashboard token → `JOBSPY_API_TOKEN`

Port 8000 is open to the internet by default and the dashboard has no login.
Set a long random token; every API call except `/health` then requires it, and
the dashboard shows an "API token" bar where you paste it once per browser
session.

```bash
openssl rand -hex 32
```

PowerShell:
`$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | % { $_.ToString('x2') })`

Skip it only if `AWS_APP_CIDRS` (step 7) restricts port 8000 to your own IP.

## 4. Assemble `AWS_APP_ENV`

One JSON object with the keys from `.env.example`. Empty strings are dropped.

```json
{
  "OPENAI_API_KEY": "sk-...",
  "TELEGRAM_BOT_TOKEN": "123456789:AAH...",
  "TELEGRAM_CHAT_ID": "123456789",
  "JOBSPY_API_TOKEN": "<64 hex chars>"
}
```

Use `NVIDIA_API_KEY` instead of (or as well as) `OPENAI_API_KEY` per step 1.
Optional: `REWRITE_API_KEY`, `OTEL_EXPORTER_OTLP_HEADERS`.

**Tracing (Langfuse):** `config.yaml` → `tracing.otel_endpoint` points at
Langfuse. Either set `OTEL_EXPORTER_OTLP_HEADERS` to
`Authorization=Basic <base64 of pk-...:sk-...>` (a raw `pk-...:sk-...` value is
encoded at startup), or set `otel_endpoint: ""` to turn export off.

Values must be strings (quote the chat id). The workflow rejects anything
that is not a JSON object. After the first deploy, editing this secret and
re-running the workflow is how keys are rotated.

## 5. AWS IAM user → `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

Use a dedicated IAM user, not the root account.

1. AWS console → IAM → Users → **Create user** → name `jobspy-lite-github`,
   no console access.
2. Permissions → **Attach policies directly** → Create policy → JSON → paste
   the policy below. Replace `us-east-1` if you will set `AWS_REGION`.
3. Attach the policy to the user.
4. User → Security credentials → **Create access key** → "Application running
   outside AWS". Copy both values now; the secret is shown once.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Ec2InRegion",
      "Effect": "Allow",
      "Action": "ec2:*",
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
    },
    {
      "Sid": "AppInstanceRole",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
        "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
        "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole", "iam:PassRole",
        "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
        "iam:TagInstanceProfile", "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": [
        "arn:aws:iam::*:role/jobspy-lite-*",
        "arn:aws:iam::*:instance-profile/jobspy-lite-*"
      ]
    },
    {
      "Sid": "AppSecret",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret",
        "secretsmanager:RestoreSecret", "secretsmanager:GetResourcePolicy",
        "secretsmanager:TagResource", "secretsmanager:UntagResource"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:jobspy-lite/env*"
    },
    {
      "Sid": "TerraformStateBucket",
      "Effect": "Allow",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::jobspy-lite-tfstate-*",
        "arn:aws:s3:::jobspy-lite-tfstate-*/*"
      ]
    },
    {
      "Sid": "WhoAmI",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

`ec2:*` is limited to one region; use an AWS account dedicated to this
project if that is still too broad for you.

## 6. SSH key pair → `TF_VAR_AWS_SSH_PUBLIC_KEY`, `AWS_SSH_KEY`

The deploy job SSHes into the instance with this key. Generate it outside the
repo with no passphrase (Actions cannot type one):

```bash
ssh-keygen -t ed25519 -N "" -C jobspy-lite -f ~/jobspy-aws
```

PowerShell: `ssh-keygen -t ed25519 -N '""' -C jobspy-lite -f $HOME\jobspy-aws`

- `jobspy-aws.pub` (one line, `ssh-ed25519 AAAA... jobspy-lite`) →
  `TF_VAR_AWS_SSH_PUBLIC_KEY`
- `jobspy-aws` (whole file including `-----BEGIN OPENSSH PRIVATE KEY-----`
  and `-----END ...-----`) → `AWS_SSH_KEY`

Keep a copy of the private key in a password manager if you want to SSH in
for debugging; otherwise delete both files after step 7.

## 7. Configure GitHub

### Secrets (required)

Settings → Secrets and variables → Actions → **Secrets** → New repository secret.

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | step 5 |
| `AWS_SECRET_ACCESS_KEY` | step 5 |
| `TF_VAR_AWS_SSH_PUBLIC_KEY` | step 6, public key line |
| `AWS_SSH_KEY` | step 6, full private key file |
| `AWS_APP_ENV` | step 4 JSON |

With the GitHub CLI (`gh auth login` first), reading from files avoids
copy-paste damage to multi-line values:

```bash
gh secret set AWS_ACCESS_KEY_ID
gh secret set AWS_SECRET_ACCESS_KEY
gh secret set TF_VAR_AWS_SSH_PUBLIC_KEY < ~/jobspy-aws.pub
gh secret set AWS_SSH_KEY < ~/jobspy-aws
gh secret set AWS_APP_ENV < ~/app-env.json
```

`gh secret set NAME` with no input prompts for the value without echoing it.

### Variables (optional)

Settings → Secrets and variables → Actions → **Variables**.

| Variable | Default | Notes |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Must match the region in the IAM policy. Pick before the first run; changing later builds a second stack. India: `ap-south-1`. |
| `AWS_INSTANCE_TYPE` | `t3.small` | `t3.micro` is free-tier eligible but slow to build. |
| `AWS_AMI_ARCH` | `amd64` | `arm64` only with Graviton types (`t4g.*`). |
| `AWS_ROOT_VOLUME_GB` | `30` | Minimum 16. |
| `AWS_APP_CIDRS` | `["0.0.0.0/0"]` | JSON list, e.g. `["203.0.113.10/32"]` to allow only your IP on port 8000. |

### `aws` environment (optional approval gate)

The apply, deploy, and destroy jobs run in an environment named `aws`. GitHub
creates it on the first run. To require approval before changes reach AWS:
Settings → Environments → `aws` → Required reviewers → add yourself.

### Actions permissions

Settings → Actions → General: Actions must be allowed, and "Workflow
permissions" must let workflows comment on pull requests if you want plan
comments (the workflow requests `pull-requests: write`).

## 8. Review `config.yaml`

Non-secret settings ship with the code; push changes to `master` to apply.
Check at least:

- `ai.provider` matches the key in `AWS_APP_ENV`
- `scrape.search_terms`, `scrape.location`, `scrape.country_indeed`,
  `scrape.sites`, `scrape.is_remote`
- `screening.experience_min_years` / `experience_max_years` and
  `score_threshold` (the Telegram alert cutoff)
- `scheduler.timezone` and `active_from` / `active_to`
- `tracing.otel_endpoint` (blank it if you skipped Langfuse in step 4)

LinkedIn and Indeed often block AWS IP addresses. If runs scrape zero jobs,
add residential proxies to `scrape.proxies` and push.

## 9. Run the workflow

Actions → **AWS** → Run workflow → branch `master`, action `apply` → Run.
(Pushing to `master` does the same.) First run takes about 10–15 minutes:

1. `bootstrap` checks the secrets, then creates the S3 state bucket
   `jobspy-lite-tfstate-<account>-<region>`.
2. `apply` creates the VPC, security group, instance, Elastic IP, IAM role,
   and the `jobspy-lite/env` secret.
3. `deploy` copies `AWS_APP_ENV` into Secrets Manager, waits for first-boot
   setup, uploads the code, builds the containers, and checks `/health`. The
   job summary shows `Dashboard: http://<IP>:8000`.

## 10. Verify and finish setup

1. Open `http://<IP>:8000`. If you set `JOBSPY_API_TOKEN`, paste it into the
   "API token" bar.
2. `http://<IP>:8000/health` (no token needed) should show `"status":"ok"`,
   `"telegram_configured":true`, `"db_ready":true`.
3. Dashboard → upload your resume (`.docx`). `/health` then shows
   `"resume_uploaded":true`.
4. Configure the schedule (interval 30–120 minutes; `config.yaml` default is
   120) and press **Start scheduler**. It stays on across restarts.
5. Press **Run now** once. Jobs appear on the Jobs board, and a Telegram
   message arrives if any score at or above the threshold.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `bootstrap` fails: "Missing repository secrets" | Add the listed secrets (names are case-sensitive). |
| `AccessDenied` / `UnauthorizedOperation` in `bootstrap` or `apply` | IAM policy missing an action, or `AWS_REGION` differs from the policy's region condition. |
| `apply` fails attaching the instance profile | IAM propagation delay. Re-run the workflow. |
| `apply` fails: secret "scheduled for deletion" | The stack was destroyed less than 7 days ago. Restore it in the Secrets Manager console or wait. |
| `deploy`: "AWS_SSH_KEY is not a valid private key" | Paste the whole private key file including BEGIN/END lines, or use `gh secret set AWS_SSH_KEY < file`. |
| `deploy`: "AWS_APP_ENV must be a JSON object" | Invalid JSON (trailing comma, smart quotes, unquoted value). Validate with `python -m json.tool app-env.json`. |
| `deploy`: health check timed out | The job prints the end of `build.log`. `t3.micro` builds can exceed 15 minutes; re-run or use `t3.small`. |
| Browser cannot reach port 8000 | `AWS_APP_CIDRS` excludes your IP. |
| Dashboard keeps asking for a token | Token in the bar does not match `JOBSPY_API_TOKEN` in `AWS_APP_ENV`. |
| `/health` shows `telegram_configured: false` | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` missing from `AWS_APP_ENV`. Fix and re-run. |
| Telegram configured but no messages | No job reached `score_threshold`, or the chat id is wrong. Re-test with step 2c. |
| Runs finish with zero jobs | Job boards blocking the AWS IP. Add `scrape.proxies`. |
| Scoring errors with 401 | Wrong or missing AI key for `ai.provider`. |

## Tear down

Actions → **AWS** → Run workflow → action `destroy`, type `jobspy-lite` in
`confirm_destroy`. This deletes the instance and its disk, including the job
database and uploaded resume. The S3 state bucket is kept; delete it in the
S3 console if you are done with AWS. Then deactivate the IAM access key.
