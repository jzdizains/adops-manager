"""Read TikTok Business-Center invite emails from a mailbox over IMAP, so the dashboard
can pull the one-time invite link (…?invite_code=…) it needs to auto-accept an invite —
the accept screen is reachable only through that emailed link (there is no in-console
"accept pending invite" surface for a first-time member; verified live 22 Sep).

Design notes / safety:
  * stdlib only (imaplib + email) — no new dependency, nothing to install.
  * credentials live on the DATA DISK (config.DATA_DIR / invite_mail.json), exactly like
    the pasted TikTok cookies — never in the repo, never in env, never rendered back.
  * every call is wrapped: a bad login / unreachable host / weird message becomes a
    readable {"error": …}, never an exception that could break a page or the sweep.
  * bounded: scans at most SCAN_CAP recent messages, reads at most BODY_CAP bytes each.
"""
from __future__ import annotations

import email
import imaplib
import json
import logging
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import config

log = logging.getLogger("adops.invite_mail")

INVITE_MAIL_FILE = config.DATA_DIR / "invite_mail.json"
SCAN_CAP = 40           # newest N messages looked at per poll
BODY_CAP = 200_000      # bytes of a single part we bother to decode
CONNECT_TIMEOUT = 20    # seconds

# The invite link carries the one-time code as ?invite_code=<hex> (seen live 22 Sep:
# business.tiktok.com/?invite_code=c062fe678b6adc984096dc184c142d1d). Email clients and
# link-trackers may wrap the URL, but the code=<hex> pair survives inside the tracked URL.
_CODE_RE = re.compile(r"invite_?code=([0-9a-fA-F]{16,64})")
# BC name, when the email states it ("… to join the Business Center "Name"")
_BC_NAME_RE = re.compile(r"Business Center[\s“\"']+([^”\"'<>\n]{2,80})", re.I)


# ---------------------------------------------------------------------------
# credentials (data disk, like the cookie file)
# ---------------------------------------------------------------------------

def save_config(host: str, user: str, password: str, port: int = 993,
                folder: str = "INBOX", use_ssl: bool = True) -> dict:
    host = (host or "").strip()
    user = (user or "").strip()
    if not host or not user or not password:
        return {"ok": False, "error": "Host, email and password are all required."}
    payload = {"host": host, "port": int(port or 993), "user": user, "password": password,
               "folder": (folder or "INBOX").strip() or "INBOX", "use_ssl": bool(use_ssl),
               "saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        from . import secrets_box
        secrets_box.write_json(INVITE_MAIL_FILE, payload)           # sealed at rest
    except OSError as e:
        return {"ok": False, "error": f"Couldn't save the mailbox settings: {e}"}
    return {"ok": True}


def load_config() -> dict:
    try:
        from . import secrets_box
        return (secrets_box.read_json(INVITE_MAIL_FILE, {}) or {}) if INVITE_MAIL_FILE.exists() else {}
    except (OSError, ValueError):
        return {}


def clear_config() -> None:
    try:
        INVITE_MAIL_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_configured() -> bool:
    c = load_config()
    return bool(c.get("host") and c.get("user") and c.get("password"))


def status() -> dict:
    """Safe to render — never includes the password."""
    c = load_config()
    return {"configured": is_configured(), "host": c.get("host", ""), "user": c.get("user", ""),
            "port": c.get("port", 993), "folder": c.get("folder", "INBOX"),
            "use_ssl": c.get("use_ssl", True), "saved_at": c.get("saved_at", "")}


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def _connect(c: dict):
    """Open + login. Raises imaplib.IMAP4.error / OSError on failure (caller wraps)."""
    host, port = c["host"], int(c.get("port", 993))
    if c.get("use_ssl", True):
        M = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=CONNECT_TIMEOUT)
    else:
        M = imaplib.IMAP4(host, port, timeout=CONNECT_TIMEOUT)
        try:
            M.starttls(ssl.create_default_context())
        except Exception:  # noqa: BLE001 — server may not offer STARTTLS; continue plain
            pass
    M.login(c["user"], c["password"])
    return M


def test_connection() -> dict:
    if not is_configured():
        return {"ok": False, "error": "No mailbox is set up yet."}
    c = load_config()
    try:
        M = _connect(c)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
        return {"ok": False, "error": _login_hint(str(e))}
    try:
        typ, _ = M.select(c.get("folder", "INBOX"), readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"Connected, but couldn't open the folder “{c.get('folder', 'INBOX')}”."}
    finally:
        _logout(M)
    return {"ok": True, "detail": f"Connected to {c['host']} as {c['user']}."}


def _login_hint(msg: str) -> str:
    m = msg.strip().splitlines()[0][:200] if msg else "login failed"
    low = m.lower()
    if "authentication" in low or "invalid" in low or "login" in low:
        return (m + " — for Gmail/Outlook use an APP PASSWORD (not your normal password), and turn on IMAP "
                "in the mail settings.")
    return m


def _logout(M) -> None:
    try:
        M.logout()
    except Exception:  # noqa: BLE001
        pass


def _decode_part(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload[:BODY_CAP].decode(charset, "replace")
    except (LookupError, ValueError, TypeError):
        return ""


def _message_text(msg) -> str:
    """text/plain + text/html of a message, concatenated (bounded)."""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                chunks.append(_decode_part(part))
            if sum(len(x) for x in chunks) > BODY_CAP:
                break
    else:
        chunks.append(_decode_part(msg))
    return "\n".join(chunks)


def _received_at(msg) -> datetime | None:
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        if d and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (TypeError, ValueError):
        return None


def find_invite_links(bc_name_hint: str = "", since_minutes: int = 60, limit: int = 10) -> dict:
    """Return {"ok": True, "invites": [{invite_code, url, subject, bc_name, received}]} — the
    newest TikTok invite links found in the mailbox within the window. Newest first. When a
    bc_name_hint is given, invites whose email names that BC sort to the front (never the sole
    filter — the code is what matters). Never raises."""
    if not is_configured():
        return {"ok": False, "error": "No invite mailbox is set up — add it in Settings.", "invites": []}
    c = load_config()
    try:
        M = _connect(c)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
        return {"ok": False, "error": _login_hint(str(e)), "invites": []}
    try:
        typ, _ = M.select(c.get("folder", "INBOX"), readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"Couldn't open the folder “{c.get('folder', 'INBOX')}”.", "invites": []}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, since_minutes))
        since_date = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")   # IMAP SINCE is date-granular
        typ, data = M.search(None, "SINCE", since_date)
        if typ != "OK":
            return {"ok": False, "error": "The mailbox rejected the search.", "invites": []}
        ids = (data[0].split() if data and data[0] else [])[-SCAN_CAP:]
        out: list[dict] = []
        seen: set[str] = set()
        hint = (bc_name_hint or "").strip().lower()
        for mid in reversed(ids):                       # newest first
            typ, msg_data = M.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            try:
                msg = email.message_from_bytes(msg_data[0][1])
            except Exception:  # noqa: BLE001
                continue
            when = _received_at(msg)
            if when and when < cutoff:
                continue
            body = _message_text(msg)
            m = _CODE_RE.search(body)
            if not m:
                continue
            code = m.group(1).lower()
            if code in seen:
                continue
            seen.add(code)
            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", "") or "")))[:200]
            bn = _BC_NAME_RE.search(subject) or _BC_NAME_RE.search(body)
            bc_name = (bn.group(1).strip() if bn else "")[:80]
            out.append({"invite_code": code,
                        "url": f"https://business.tiktok.com/?invite_code={code}",
                        "subject": subject, "bc_name": bc_name,
                        "received": when.isoformat() if when else ""})
            if len(out) >= max(1, limit) * 3:
                break
    finally:
        _logout(M)
    if hint:
        out.sort(key=lambda x: (hint not in (x["bc_name"] or "").lower()
                                and hint not in (x["subject"] or "").lower()))
    return {"ok": True, "invites": out[:max(1, limit)]}
