# SAM Radar

SAM Radar is a shareable, dependency-free opportunity intelligence dashboard for Fearless Solutions. It reads federal opportunities from SAM.gov, applies transparent fit scoring, and can send one deduplicated daily email brief.

It uses Python's standard library only. No `pip install`, framework, client-side build, or external database is required.

## What is better in this rebuild

The previous implementation made five NAICS searches in parallel. That can consume a personal SAM.gov key's request budget quickly and trigger throttling. This version instead:

- makes small, overlapping incremental searches (three days by default) after the one-time backfill;
- has one process-wide request pace clock, including across overlapping jobs;
- honors `Retry-After` for HTTP 429 and expands the pace before retrying;
- commits each NAICS/date slice as it completes, so an interrupted backfill is recoverable and no full-day fetch is repeated;
- records every sync run, request count, inserted/updated/unchanged counts, and failure;
- performs a 90-day historical backfill only when explicitly requested.

The application binds to `0.0.0.0` and honors a host-provided `PORT`, so it works on a cloud web service as well as a local laptop. The provided Dockerfile has no dependency-install step. A password gate and environment-variable secrets make it safe to share with a small team at a custom domain.

## Run locally

```sh
python3 server.py
```

Open `http://127.0.0.1:8765`. On first launch the dashboard loads clearly identifiable sample data. Add your SAM.gov key in **Settings**, then run **Backfill 90d** once. After that, use **Sync now** or enable the daily incremental sync.

The Mac launcher is `run_mac.command`; make it executable once with:

```sh
chmod +x run_mac.command
```

## Cloud deployment: Render + your domain

Render is a good fit for this small single-process app because it runs Docker services, gives each web service a public URL, supports custom domains, and can attach persistent storage. The Docker image itself remains standard-library-only.

1. Put these files in a private GitHub repository and create a Render **Web Service** from it. Select the Docker runtime, or create it from the supplied `render.yaml` Blueprint.
2. Before relying on the first deploy, attach a Render persistent disk at `/var/data`. SQLite and `settings.json` live there; without a disk, a redeploy loses them. Keep this app to **one instance** because SQLite is a single-file database.
3. In Render's Environment section, set these secret variables (do not commit them):

   | Variable | Required | Purpose |
   | --- | --- | --- |
   | `SAM_API_KEY` | Yes for live data | Your SAM.gov API key |
   | `RADAR_ACCESS_PASSWORD` | Yes for sharing | Browser password for the whole dashboard |
   | `RADAR_PUBLIC_URL` | Recommended | `https://radar.yourdomain.com` |
   | `RADAR_SMTP_USERNAME` | If email is used | SMTP login |
   | `RADAR_SMTP_PASSWORD` | If email is used | Gmail App Password or SMTP password |
   | `RADAR_FROM_ADDRESS` | If email is used | Sender address |
   | `RADAR_TO_ADDRESSES` | If email is used | Comma-separated recipients |

4. Visit the generated `onrender.com` URL, enter the sharing password as the password for username `radar`, and confirm the dashboard loads. `GET /healthz` is intentionally public and contains only `{"ok": true}` for Render's health check.
5. In the service's **Settings → Custom Domains**, add the domain you own, such as `radar.fearlesssolutions.com`. Render will show the exact DNS record required by your registrar. Create that record, wait for verification, then set `RADAR_PUBLIC_URL` to the new `https://` address and redeploy.
6. In Settings, enable scheduled sync and email only after the live SAM connection and a test digest both work.

Render documents that web services must bind to `0.0.0.0`, use a host-provided port, can use custom domains, and support a Docker runtime; this server does all of those. It also documents that the filesystem is ephemeral unless you attach a persistent disk, which is essential for this SQLite design. See [Render web services](https://render.com/docs/web-services), [persistent disks](https://render.com/docs/disks), and [custom domains](https://render.com/docs/custom-domains).

## Security notes

- Always set `RADAR_ACCESS_PASSWORD` before exposing the service. The dashboard uses HTTP Basic authentication; use the username `radar` and the password you set.
- Prefer the environment variables above to entering secrets in the dashboard. Environment values override disk settings at runtime.
- Keep the repository private. `.gitignore` excludes the live database and local `settings.json`.
- The hosting provider terminates HTTPS for the custom domain. Do not point public DNS directly at a home computer.
- Back up the persistent disk/database on a regular cadence before relying on it as a capture record of truth.

## Operational model

| Task | How it runs |
| --- | --- |
| First historical data | Click **Backfill 90d** once; paced seven-day chunks × NAICS codes |
| Daily data | Scheduler runs the small incremental window; overlap catches late SAM corrections |
| Team access | Cloud URL + host-managed HTTPS + password gate |
| New-opportunity email | Each `notice_id` + reason is recorded after a successful email; no repeats |
| Quiet-day evidence | The scheduled email sends a heartbeat so silence is meaningful |

## Files

- `server.py` — HTTP server, SQLite, SAM client, rate limiting, scoring, scheduler, email.
- `index.html` — responsive dashboard; no JavaScript framework or build step.
- `Dockerfile` — cloud container; no package installer.
- `render.yaml` — optional Render Blueprint with secret placeholders.
- `settings.example.json` — non-live configuration shape.

