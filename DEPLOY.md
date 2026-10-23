# Deploying JobSpy Lite

## Option A — Docker Compose (recommended)

```bash
cd jobspy-lite
cp .env.example .env      # then edit: NVIDIA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
docker compose up --build -d
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"ok","ai_model":"...","scheduler_running":false,
#  "resume_uploaded":false,"telegram_configured":true,"db_ready":true}
```

Open **http://localhost:8000** and finish setup in the UI:

1. Upload your resume (.docx) on the Dashboard
2. Set your score threshold + experience window
3. Configure the schedule (interval or cron) and press **Start scheduler**

Everything (SQLite DB, Excel export, resume) persists in `./data` on the host
and survives rebuilds and restarts.

### Common commands

```bash
docker compose logs -f          # tail the logs (scrape/scheduler activity shows here)
docker compose restart          # restart without rebuilding
docker compose up --build -d    # rebuild after code changes
docker compose down             # stop (data volume stays)
```

### Notes

- **Windows/macOS:** start Docker Desktop first — the daemon must be running.
- **Datacenter IPs get blocked** by LinkedIn/Indeed. If the container scrapes
  zero jobs, put residential proxies in `.env` (`PROXIES=http://user:pass@host:port,...`)
  and `docker compose up -d` again.
- **Scheduler:** it only runs when you press Start (or `POST /scheduler/toggle?enabled=true`).
  The `enabled` flag is stored in the DB, so it auto-resumes after restarts.
- **Changing the port:** edit `ports:` in `docker-compose.yml` (e.g. `"9000:8000"`).

## Option B — Any VPS with Docker (DigitalOcean / Hetzner / EC2 / …)

```bash
# on the server, after cloning the repo
cp .env.example .env && nano .env          # fill in keys
docker compose up --build -d
```

Then open `http://<server-ip>:8000`. For HTTPS, put Caddy or nginx in front:

```bash
# example: Caddy in the same directory
docker run -d --name caddy --network host \
  -v caddy_data:/data caddy:2 \
  caddy reverse-proxy --from jobs.example.com --to 127.0.0.1:8000
```

(DNS for `jobs.example.com` must point at the server first.)

## Option C — No Docker (local machine or bare server)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
cp .env.example .env                 # fill in keys
python run.py --serve --host 0.0.0.0 --port 8000
```

`run.py --serve` also starts the cron scheduler (if it was left enabled) and
serves the built dashboard from `frontend/dist`.

## Option D — Run one pass only (no server, e.g. under Windows Task Scheduler / Unix cron)

```bash
python -m app.cli --once
```

Scrapes, scores, stores to SQLite, refreshes `data/jobs.xlsx`, and sends any
pending Telegram alerts — then exits. Point your OS scheduler at this command.

## Option E — AWS (Terraform in GitHub Actions)

AWS is deployed only through the **AWS** workflow (`.github/workflows/aws.yml`).
Terraform runs in Actions with state in S3, and all configuration lives in
GitHub secrets and variables:

1. Add the repository secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `TF_VAR_AWS_SSH_PUBLIC_KEY`, `AWS_SSH_KEY`, and `AWS_APP_ENV` (a JSON object
   of the `.env` keys).
2. Actions → **AWS** → Run workflow → `apply`.

After that, every push to `master` applies Terraform and redeploys the app.
Pull requests that touch `infra/aws` get a plan comment. Details, variables,
IAM permissions, and teardown: `infra/aws/README.md`.

## First-run checklist

- [ ] `.env` has NVIDIA_API_KEY (+ Telegram token/chat id for alerts)
- [ ] `docker compose up --build -d` succeeds and `/health` returns `ok`
- [ ] Resume uploaded (dashboard)
- [ ] Scheduler started (dashboard) — dot turns green with a next-run time
- [ ] First manual **Run now** succeeds; jobs appear on the Jobs board
- [ ] Telegram alert arrives for jobs above your threshold
