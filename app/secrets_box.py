"""Secrets encrypted at rest (AES-256-GCM).

What's covered: every TikTok access / refresh token (ad accounts and Business Centers), each
user's TOTP secret, the Events API token in settings, the ads.tiktok.com cookie file and the
invite mailbox password file. A copy of the database or the data disk no longer drives the
ad accounts on its own — the key lives in the environment, not on the disk.

Key: SECRETS_KEY (any string; 32 random bytes as base64 is ideal). When it isn't set, a key is
derived from SESSION_SECRET (HKDF-SHA256) — also an environment variable, never on the disk.
Rotating: put the old value in SECRETS_KEY_OLD (comma-separated); values it opens are
re-sealed with the new key at the next start.

Format: "enc:v1:" + base64url(nonce ‖ ciphertext ‖ tag). Anything without the prefix is a
value from before encryption and is read as is (and sealed at the next start). A value that
won't open (wrong key) is returned sealed, never as "" — so a lost key can never read as
"2FA not set up" — and is reported once on Diagnostics.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from functools import lru_cache

try:
    from sqlalchemy.types import Text, TypeDecorator
except ImportError:          # scripts / tests without SQLAlchemy: the file helpers still work
    Text, TypeDecorator = None, object

PREFIX = "enc:v1:"
_log = logging.getLogger("adops.secrets")
_warned = threading.Event()


def _derive(material: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    raw = material.encode()
    try:                                                   # a proper 32-byte key given as base64 is used as is
        b = base64.urlsafe_b64decode(material + "=" * (-len(material) % 4))
        if len(b) == 32:
            return b
    except (ValueError, TypeError):
        pass
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"adops-secrets-v1", info=b"at-rest").derive(raw)


@lru_cache(maxsize=1)
def _keys() -> tuple[bytes, ...]:
    """(current key, old keys…). Empty when cryptography is missing (then nothing is sealed)."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        _log.error("cryptography isn't installed — secrets are stored unencrypted")
        return ()
    from . import config
    cur = os.environ.get("SECRETS_KEY", "").strip() or f"session:{getattr(config, 'SESSION_SECRET', '')}"
    olds = [k.strip() for k in os.environ.get("SECRETS_KEY_OLD", "").split(",") if k.strip()]
    # an old entry may be an old SECRETS_KEY or an old SESSION_SECRET (installs without a
    # SECRETS_KEY seal with "session:<SESSION_SECRET>") — try both readings of each
    keys = [cur]
    for k in olds:
        keys += [k, f"session:{k}"]
    return tuple(_derive(k) for k in dict.fromkeys(keys))


def reset_keys() -> None:          # tests
    _keys.cache_clear()
    _open.cache_clear()


def enabled() -> bool:
    return bool(_keys())


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value):
    """Encrypt a string (None / "" / already sealed pass through)."""
    if value is None or value == "" or not isinstance(value, str) or is_sealed(value):
        return value
    keys = _keys()
    if not keys:
        return value
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(keys[0]).encrypt(nonce, value.encode(), b"adops")
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


@lru_cache(maxsize=4096)
def _open(value: str) -> tuple[str, bool]:
    """(plaintext, opened_with_current_key). Raises ValueError when no key opens it."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    body = value[len(PREFIX):]
    blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    nonce, ct = blob[:12], blob[12:]
    for i, k in enumerate(_keys()):
        try:
            return AESGCM(k).decrypt(nonce, ct, b"adops").decode(), i == 0
        except InvalidTag:
            continue
    raise ValueError("no key opens this value")


def unseal(value):
    """Decrypt; plaintext (legacy) passes through; an unopenable value comes back sealed."""
    if not is_sealed(value):
        return value
    if not _keys():
        return value
    try:
        return _open(value)[0]
    except (ValueError, TypeError):
        if not _warned.is_set():
            _warned.set()
            _note("A stored secret couldn't be decrypted — SECRETS_KEY (or SESSION_SECRET) changed. "
                  "Put the previous value in SECRETS_KEY_OLD and restart, or reconnect TikTok.")
        return value


def needs_reseal(value) -> bool:
    """Plaintext, or sealed with an old key."""
    if value is None or value == "" or not isinstance(value, str):
        return False
    if not is_sealed(value):
        return enabled()
    try:
        return not _open(value)[1]
    except (ValueError, TypeError):
        return False


def _note(msg: str) -> None:
    _log.error(msg)
    try:
        from .database import SessionLocal
        from . import queries
        d = SessionLocal()
        try:
            queries.log(d, msg, level="error", source="secrets")
            d.commit()
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        pass


class EncryptedText(TypeDecorator):
    """A Text column stored sealed and read back in the clear — transparent to the code."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value)

    def process_result_value(self, value, dialect):
        return unseal(value)


# ---------------------------------------------------------------------------------------
# files on the data disk

def write_json(path, obj) -> None:
    text = json.dumps(obj, indent=2)
    sealed = seal(text)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(sealed)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def read_json(path, default=None):
    try:
        raw = path.read_text()
    except OSError:
        return default
    raw = unseal(raw.strip()) if raw.strip().startswith(PREFIX) else raw
    try:
        return json.loads(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------------------
# one-time sealing of what was stored before (runs at every start; cheap when done)

COLUMNS = (("ad_accounts", "access_token"), ("ad_accounts", "refresh_token"),
           ("business_centers", "access_token"), ("users", "totp_secret"))


def seal_existing(engine) -> dict:
    """Seal plaintext (or old-key) values in place with raw SQL. Returns {table.col: n}."""
    from sqlalchemy import inspect, text
    if not enabled():
        return {}
    out: dict = {}
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, col in COLUMNS:
            if table not in tables or col not in {c["name"] for c in insp.get_columns(table)}:
                continue
            rows = conn.execute(text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''")).fetchall()
            n = 0
            for rid, val in rows:
                if not needs_reseal(val):
                    continue
                plain = unseal(val)
                if is_sealed(plain):
                    continue                                   # won't open: leave it for the operator
                conn.execute(text(f"UPDATE {table} SET {col} = :v WHERE id = :i"), {"v": seal(plain), "i": rid})
                n += 1
            if n:
                out[f"{table}.{col}"] = n
    return out


def seal_files() -> list[str]:
    """The cookie file and the invite mailbox file, sealed if they're still plain."""
    from . import config
    done = []
    paths = [config.COOKIE_FILE, config.DATA_DIR / "invite_mail.json"]
    for p in paths:
        try:
            if p.exists():
                raw = p.read_text()
                if raw.strip() and not raw.strip().startswith(PREFIX) and enabled():
                    obj = json.loads(raw)
                    write_json(p, obj)
                    done.append(p.name)
        except (OSError, ValueError):
            continue
    return done


SEALED_SETTINGS = ("events_access_token", "notify_telegram_token")


def settings_seal(data: dict, keys=SEALED_SETTINGS) -> dict:
    return {k: (seal(v) if k in keys and isinstance(v, str) else v) for k, v in data.items()}


def settings_unseal(data: dict, keys=SEALED_SETTINGS) -> dict:
    return {k: (unseal(v) if k in keys and isinstance(v, str) else v) for k, v in data.items()}


def fingerprint(value: str) -> str:
    """A short, non-reversible tag for a secret (logs / "which token is this")."""
    if not value:
        return ""
    return hashlib.sha256(("adops-fp:" + value).encode()).hexdigest()[:12]
