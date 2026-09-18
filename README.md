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
6. The Instant Page builder needs Chromium: it installs itself into
   `$DATA_DIR/pw-browsers` on first use (Render drops the build cache between
   deploys, so the build command deliberately does not install it).
7. **Which build is running?** Settings → Server shows `running build <7 hex chars>`
   (also on Diagnostics and in `/diagnostics.json` as `"build"`). It is a fingerprint
   of the code files; `BUILD.txt` at the root of each delivered zip holds the value
   that zip should show once its deploy has finished.

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

## Instant Pages on many accounts

An Instant Page belongs to one ad account. **Instant Pages › Clone to…** copies a
finished (published) page onto another account, or onto every account of a Business
Center, through the page editor's own web session (the TikTok Cookies page must be set
up): read → create as a duplicate → optionally re-point the button link → publish, then
verified through the official `/page/get/`. Clone to ONE account first and open the
copy in Ads Manager. A launch onto an account that lacks the preset's page copies it
from an account that has it (same Business Center first); if no account has it, the
page template builder is the fallback. `tools/dupe-pages.mjs` is the same flow as a
standalone Node script (cookie.txt + accounts.txt; both are git-ignored).

## Super Launcher › Profile videos

Instead of pasting a spark code, pick posts straight from the profiles a Business Center
shares (BC → Assets → TikTok accounts): every post of every profile is listed with its
cover and caption, multi-select, and the picked posts are spread over the accounts
(accounts per video). Each pick becomes a spark row keyed by the post's item id and runs
under the profile's own identity on each account — no auth code involved.

## Super Launcher › The creative board (v123)

Step 2 has one "Choose myself…" mode. Everything you pick lands on a board of tiles
(cover, source badge, type, order number; click = preview), from any source, mixed:

* **Library** — your uploaded videos and carousels (the usual picker).
* **Profile posts** — the Business Centers' shared profiles, with covers.
* **Spark codes** — several at once (the spark picker now multi-selects).
* **Upload videos** — the files go into the library *and* onto the board, with an upload bar.
* **Paste spark codes** — saved to Spark codes and put on the board (known codes too).

The picks are spread over the selected accounts in the board's order. *Accounts per
creative* `0` (the default) spreads them evenly — one pick means every account gets it,
like the old spark mode; `N` gives each pick N accounts and caps the accounts to what the
picks cover. Library picks go through the library path (preset text, one creative per
account, reuse allowed), spark and profile picks through the spark path; a mixed board is
one batch with one result page, and *Retry failed* relaunches the same creative or post on
the same account (the recipe keeps both per-account maps).

"Fresh videos" / "Fresh carousels" show the next unused items as a strip, in the order
they will be used, before anything runs.

**Autosave.** The launcher's state (preset, board, accounts, options, step) is saved to the
server per user 1.5 s after any change (`launch_draft:u<id>` in the settings table, ≤ 96 KB)
and offered back as a *Resume* banner on the next visit; it clears itself on launch or
Discard. Arriving with `?creatives=`, `?spark=`, `?accounts=` or `?bc=` skips the offer.

## Landers (v124)

Creatives › **Landers** builds pages for **your own domains** from templates. A lander is a
row (`models.Lander`: slug, template, settings JSON); **⬇ Package** exports one self-contained
`index.html` (+ `robots.txt`) with the settings baked in as `window.LANDER_CFG` and the shared
runtime (`static/lander.js`) and click script (`static/pass-source.js`) inlined. At open the
page re-reads `/t/l/<slug>.json` (public, uncached, CORS open) for the *live* part — the next
URL, the routing rules, the escape method and the pixel event — so those change here without
re-uploading. Texts, colours and the pixel code are baked: re-download after changing them.

The runtime (`window.L`): in-app browser and platform detection (`L.env`), the ad's ids and a
stable visitor id (`L.params`), one `sendBeacon` per step to `/t/lp` under the slug (view /
engaged / continue / escaped / escape_miss / gate / route / cta, with the in-app flag and
platform — the Landers page shows the last 7 days per page), `L.pixel()` that only fires when a
template asks (never on paint), rules → URL (`L.route()`: platform, in-app, age bracket,
country from the edge header when the host sets one, local hours; first match wins, else the
next URL), and `L.escape()` that tries the chosen escape-test method and, if the page is still
visible 1.2 s later, continues **in-app to the same URL** — no error screens, no decoys, no
deep-link traps. `L.age.bracket(year)` / `L.setAge(year)` serve an age step (next template).

Templates so far: **Open-in-browser prelander** — in the TikTok app it offers to open the next
page in the phone's real browser; in a real browser it continues on the tap. Beacons for a
slug are accepted only while the lander exists and is enabled (`funnel.accept`).

## Events API › value + `_ttp` (v125)

Settings › Events API gained **Event value**: *the postback's payout* (default) or *a fixed
amount per event* (e.g. 6.00), optionally only for events whose source name or click
landing URL contains one of the match words (so one lander's conversions carry a flat value
while the rest report the payout). The dashboard's forwarder already does what a PHP
"postback bridge" does — the network's postback arrives, the dashboard POSTs the event to
`/event/track/` with the click's ttclid, ip and user agent — so no PHP file with a token in
it is needed on the lander host. `pass-source.js` now also reads the pixel's `_ttp` cookie on
the lander and registers it with the click; the Events API gets it as a second identifier.
The conversion event (CompleteRegistration on a registration campaign) is sent **only**
server-side; the Playful lander fires Page view + LandingPageView + ViewContent on landing
and ClickButton on the CTA, nothing else.

## Events API › match signals (v126)

Every server-side event now carries every match signal the visit can give without asking
the visitor for anything: the click's **ttclid**, the pixel's **_ttp** cookie, the lander's
stable anonymous visitor id hashed as **external_id** (SHA-256 of the trimmed, lower-cased
id — the same value the page's `ttq.identify()` sends, so the browser and server events
join into one person), the real visit's **ip** and **user agent**, the **page URL** and its
**referrer**, and a unique **event_id** (the transaction id). `pass-source.js` mints or reads
the visitor id (`tmp_vid`, handed over as `?svid=` between pages) and registers it with the
click; a lander on a tracker's script instead (ClickFlare) gets the visitor id and referrer
from its funnel beacon, which now carries the ttclid value (lander v7). The P&L "sent" pill
lists which signals went with each event.

## Events API › on landing page view (v127)

Settings › Events API › **On landing page view**: when on, every lander VIEW beacon with a
TikTok click id fires a server-side event (default CompleteRegistration, fixed value, the
pages it applies to as a comma list — `play` for the Playful lander, or a kit lander's slug)
with the click id, the hashed visitor id, the visit's IP and browser, page URL and referrer;
one per visitor per page (`event_id = lpv-<page>-<visitor id>`). Sent by one background
thread through a bounded queue (`app/lpv_events.py`), so the beacon still answers at once.
The offer's real conversions keep coming through the postback path; TikTok will optimise
the campaign for visits once this is on.
