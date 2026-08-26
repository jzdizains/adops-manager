# AdOps Manager

A single-operator web dashboard for running TikTok ad campaigns at scale across
many ad accounts (100–250) under one TikTok Business Center. Launch a preset to
dozens of accounts in one click, monitor status/spend everywhere, manage Spark
codes, wire up pixels — without touching TikTok Ads Manager account by account.

Server-rendered FastAPI + Jinja2 + SQLite + vanilla JS. No build step, nothing
to compile — deliberately easy to read and fix.

## Quick start (local)

```bash
pip install -r requirements.txt
export APP_PASSWORD=pickapassword
export SESSION_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with `APP_PASSWORD`. Everything except
the TikTok connection works immediately (presets, cookies page, etc.).

## What YOU must supply (none of this ships with the code)

1. **A TikTok Marketing API app** — create one in the
   [TikTok for Business developer portal](https://business-api.tiktok.com/portal).
   Scopes needed: Ad Account Management, Campaign, Ad Group, Ad, Reporting,
   Creative/Identity (Spark), Pixel, Lead, Business Center.
2. **A registered Redirect URI** on that app matching your deploy domain
   EXACTLY, character-for-character: `https://<your-domain>/oauth/callback`.
3. **Sandbox → Production** — a fresh app starts in Sandbox and can only be
   authorized by the developer's own account plus whitelisted test users. Add
   your own TikTok account as a test user for immediate use, or submit the app
   for Production review.
4. **Your Business Center**, ad accounts, and creator (Spark) account.
5. **Your TikTok web cookies** (paste on the TikTok Cookies page, or push them
   with a Cookie-Editor-style Chrome extension to `POST /cookies/push`) — only
   needed for web-only features: instant-page cloning and some lead-form reads.

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `TIKTOK_APP_ID` / `TIKTOK_APP_SECRET` | yes (for TikTok) | Your Marketing API app credentials |
| `OAUTH_REDIRECT_URI` | yes (for TikTok) | Must match the app registration exactly |
| `APP_PASSWORD` | yes | The single operator login |
| `SESSION_SECRET` | yes | Signs the session cookie |
| `DATA_DIR` | prod: yes | Persistent disk path (Render: `/data`) — SQLite + cookies live here |
| `BUSINESS_TZ` | no | Business timezone (default `America/New_York`) |
| `SECURITY_PIN` | no | Optional PIN gate for sensitive actions; empty = disabled |

Never commit any of these.

## Deploying on Render

1. Push this repo to a **private** GitHub repo.
2. In Render: New → Blueprint → point it at the repo (`render.yaml` does the rest),
   or create a Web Service manually with build `pip install -r requirements.txt`
   and start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
3. **Mount a persistent disk at `/data`** and set `DATA_DIR=/data`. Without the
   disk, every deploy wipes the database and stored cookies.
4. Set the env vars above in the Render dashboard.
5. Register `https://<your-render-domain>/oauth/callback` as the Redirect URI
   on your TikTok app, set `OAUTH_REDIRECT_URI` to the same value, then open
   the app → avatar menu → **Connect TikTok**.
6. If you keep the Playwright-based web-only features, keep
   `playwright install chromium` in the build command (already in render.yaml).

## First-run checklist

1. Log in → avatar menu → **Connect TikTok** → authorize → accounts sync automatically.
2. **Presets** → create your first preset. ABO vs CBO matters: CBO budget is
   stored on the preset itself, per-ad-group budget only applies to ABO.
3. **Spark Codes** → “Auto-grab from creators” pulls every ad-authorized post
   from creators connected to your accounts/BC (only ad-authorized posts are
   listable — that's a TikTok API limit, not a bug).
4. **Super Launcher** → tick accounts, pick the preset, launch. The result page
   shows per-account success/failure with copyable technical detail.
5. **Status** → “Sync now” caches campaigns + today's spend for the dashboards.
6. For pixel presets: the optimization event must ALREADY exist on the pixel —
   fire it once on the live page before optimizing on it.

## Architecture in 60 seconds

```
app/main.py              FastAPI app, session auth middleware, router registry
app/config.py            env-driven settings
app/database.py          engine + create_all + add-missing-column light migrations
app/models.py            ~17 SQLAlchemy tables (AdAccount, Template, SparkCode, …)
app/tiktok_api.py        thin httpx wrapper over Marketing API v1.3 ({code,message,data})
app/spark_web_api.py     cookie-authenticated ads.tiktok.com calls (2nd auth path)
app/error_messages.py    TikTok error code → plain-English fix
app/routes/launch.py     preset → launch-field synthesis + objective→optimization map
app/routes/campaigns.py  THE launch engine (payloads, dupes, ladders, spark resolve, pixel)
app/routes/…             one router per page (super launcher, status, performance, …)
app/templates/           Jinja2 pages; base.html carries the top nav
app/static/style.css     dark base theme + tokens
app/static/topnav.css    light PRODUCTION theme — loaded last, its :root wins
```

### Rules baked in from hard-won lessons (do not undo these)

- CBO lives on the **Template row columns**, never in the `adgroup_settings` JSON.
- Spark resolution never guesses: exact code match → identity that LISTS the
  item → shared `BC_AUTH_TT` identity → refuse with a clear error.
- Cookie validation accepts the `_ads` SSO cookie family, not just `sessionid`;
  `csrftoken` always required.
- A 40002/40102 on a cookie probe means *no permission*, **not** expiry.
  Genuine expiry = login redirect / HTML response / code 200000.
- Pixel ad groups: numeric `pixel_id` (resolved+cached from the code), an
  `optimization_event` that already exists on the pixel, and **no**
  `promotion_website_type` field.
- Placements are hardcoded TikTok-only (`PLACEMENT_TIKTOK`) — no Pangle.
- Bump `STATIC_VERSION` in `config.py` to cache-bust CSS/JS after changes.
- SQLite + pasted cookies persist under `DATA_DIR` — mount a disk in prod.
