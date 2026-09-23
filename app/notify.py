"""Error alerts pushed to Telegram and/or email — so a suspended account or a failed launch
doesn't wait for someone to open the dashboard.

Per user (Settings › Alerts): a Telegram bot token + chat id, and/or an email address; and
which alerts go out (errors only, or errors + warnings). Every sweep, the new Alert rows that
user can see (inbox.alert_visible — the same rule as their Inbox) are sent as ONE message
(at most MAX_LINES lines). A pointer per user (Setting "notify_ptr:u<id>") remembers the last
alert sent, so nothing is sent twice and turning it on doesn't replay the past.

Email goes through the server's SMTP (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD /
SMTP_FROM environment variables); without them the email channel says "not configured".
The bot token is sealed at rest like every other secret (secrets_box).
"""
from __future__ import annotations

import os

SCAN_MAX = 5000           # rows looked at per user per pass (after long downtime the rest is skipped)
MAX_LINES = 15
TELEGRAM_API = "https://api.telegram.org"


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def levels_for(setting: str) -> tuple[str, ...]:
    return ("err", "warn") if setting == "warn" else ("err",)


def compose(alerts: list, app_name: str = "Dashboard", base_url: str = "") -> tuple[str, str]:
    """(subject, text) for a batch of alerts. Pure."""
    n = len(alerts)
    errs = sum(1 for a in alerts if getattr(a, "level", "") == "err")
    subject = f"{app_name}: {n} alert{'s' if n != 1 else ''}" + (f" ({errs} error{'s' if errs != 1 else ''})" if errs else "")
    lines = []
    for a in alerts[:MAX_LINES]:
        mark = "🔴" if getattr(a, "level", "") == "err" else "🟠"
        msg = (getattr(a, "message", "") or "").strip().replace("\n", " ")
        lines.append(f"{mark} {msg[:300]}")
    if n > MAX_LINES:
        lines.append(f"…and {n - MAX_LINES} more")
    if base_url:
        lines.append(f"\n{base_url.rstrip('/')}/inbox")
    return subject, "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str, client=None) -> tuple[bool, str]:
    import httpx
    if not token or not chat_id:
        return False, "Telegram isn't set up (bot token and chat id)."
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0))
    try:
        r = client.post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code == 200 and d.get("ok"):
            return True, ""
        return False, f"Telegram said: {d.get('description') or ('HTTP ' + str(r.status_code))}"
    except httpx.HTTPError as e:
        return False, f"Couldn't reach Telegram: {type(e).__name__}"
    finally:
        if own:
            client.close()


def send_email(to: str, subject: str, text: str) -> tuple[bool, str]:
    import smtplib
    import ssl
    from email.message import EmailMessage
    if not to:
        return False, "No email address set."
    if not smtp_configured():
        return False, "Email isn't configured on the server (SMTP_HOST / SMTP_FROM)."
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_FROM"], to, subject
    msg.set_content(text)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls(context=ssl.create_default_context())
        with s:
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True, ""
    except (smtplib.SMTPException, OSError) as e:
        return False, f"Email failed: {type(e).__name__}: {str(e)[:120]}"


def channels(settings: dict) -> dict:
    return {"telegram": bool(settings.get("notify_telegram_token") and settings.get("notify_telegram_chat")),
            "email": bool(settings.get("notify_email_to"))}


def deliver(settings: dict, subject: str, text: str) -> list[str]:
    """Send to every configured channel. Returns the errors (empty = all sent)."""
    errs = []
    ch = channels(settings)
    if ch["telegram"]:
        ok, e = send_telegram(settings["notify_telegram_token"], settings["notify_telegram_chat"], f"{subject}\n\n{text}")
        if not ok:
            errs.append(e)
    if ch["email"]:
        ok, e = send_email(settings["notify_email_to"], subject, text)
        if not ok:
            errs.append(e)
    return errs


def dispatch(db) -> int:
    """Background: each user's new visible alerts, one message per user. Returns messages sent.

    The pointer moves to the newest alert id read at the START of the pass and only rows up to it
    are sent (a row committed mid-pass goes out next time, once). Every row up to it is looked at
    (in pages), so a flood of other people's alerts can't push this user's ones past the pointer.
    Switched-off users keep their pointer moving, so switching on never replays the past. One
    user's broken channel never stops the others."""
    from . import config, inbox, models, queries, scope as scope_mod, settings_store
    sent = 0
    top = db.query(models.Alert.id).order_by(models.Alert.id.desc()).first()
    top_id = int(top[0]) if top else 0
    for u, us, ids in settings_store.per_user(db):
        key = f"notify_ptr:u{u.id}"
        try:
            raw = queries.get_setting(db, key, "")
            if not any(channels(us).values()):
                if raw and int(raw or 0) < top_id:
                    queries.set_setting(db, key, str(top_id))     # off: keep up, so "on" starts from now
                continue
            if not raw:                                 # first time on: start from now, never replay
                queries.set_setting(db, key, str(top_id))
                continue
            ptr = int(raw or 0)
            if top_id <= ptr:
                continue
            sc = scope_mod.Scope(mode="user", ids=set(ids), user_id=u.id)
            lv = levels_for(us.get("notify_level") or "err")
            new, cur, scanned = [], ptr, 0
            while cur < top_id and scanned < SCAN_MAX:
                page = (db.query(models.Alert).filter(models.Alert.id > cur, models.Alert.id <= top_id,
                                                      models.Alert.level.in_(lv))
                        .order_by(models.Alert.id).limit(500).all())
                if not page:
                    break
                scanned += len(page)
                cur = page[-1].id
                new.extend(a for a in page if inbox.alert_visible(a, sc))
            queries.set_setting(db, key, str(top_id))  # advance first: a failing channel never re-sends a flood
            if not new:
                continue
            subject, text = compose(new, config.APP_NAME, os.environ.get("PUBLIC_URL", ""))
            errs = deliver(us, subject, text)
        except Exception as e:      # noqa: BLE001 — one user's bad address / token never stops the rest
            db.rollback()
            errs = [f"{type(e).__name__}"]
        if errs:
            try:
                queries.log(db, f"alert notification for {u.email} failed: " + " · ".join(errs), level="warning", source="notify")
                db.commit()
            except Exception:      # noqa: BLE001
                db.rollback()
        else:
            sent += 1
    return sent
