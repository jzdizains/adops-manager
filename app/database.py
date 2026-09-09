"""Engine, SessionLocal, Base, init_db + light migrations.

Migration approach (per the guide §4): `Base.metadata.create_all()` creates any
NEW tables, then a hand-rolled `schema_additions` map adds any NEW columns to
existing tables via `ALTER TABLE ... ADD COLUMN`, so schema changes land on the
live SQLite DB without wiping data.
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15} if config.DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if config.DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event as _event

    @_event.listens_for(engine, "connect")
    def _sqlite_tuning(dbapi_conn, _record):
        """Several users + the background sweeps share one SQLite file. WAL lets pages
        read while a sweep writes (the default journal makes every reader wait for the
        writer), busy_timeout waits instead of failing, NORMAL sync is safe under WAL."""
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA temp_store=MEMORY")
        finally:
            cur.close()


# table -> {column: SQL type} — add new columns here when models grow.
# Everything currently in models.py is covered by create_all; entries below
# exist so FUTURE columns can be added without wiping the live DB.
schema_additions: dict[str, dict[str, str]] = {
    # example: "templates": {"campaign_name_pattern": "TEXT DEFAULT ''"},
    "templates": {"campaign_name_pattern": "TEXT DEFAULT ''"},
    "jobs": {"cancel_requested": "BOOLEAN DEFAULT 0", "quiet": "BOOLEAN DEFAULT 0"},
    "ad_accounts": {"balance": "REAL DEFAULT 0", "enabled": "BOOLEAN DEFAULT 1",
                    "error_count": "INTEGER DEFAULT 0", "cooldown_until": "DATETIME"},
    "spark_codes": {"use_count": "INTEGER DEFAULT 0", "source": "TEXT DEFAULT ''"},
    "launch_queue": {"use_library": "BOOLEAN DEFAULT 0"},
    # Events API loop: per-event postbacks land on live DBs untouched
    "postback_events": {
        "txn": "TEXT DEFAULT ''",
        "ttclid": "TEXT DEFAULT ''",
        "event": "TEXT DEFAULT ''",
        "forward_status": "TEXT DEFAULT ''",
        "click_id": "TEXT DEFAULT ''",
    },
    # Phase 2 (sources/P&L): new columns on live DBs
    "launch_logs": {
        "spark_code_id": "INTEGER",
        "source": "TEXT DEFAULT ''",
        "landing_url": "TEXT DEFAULT ''",
        "optimization_event": "TEXT DEFAULT ''",
    },
    # Phase 1 (multi-BC monitor): metric columns land on live DBs untouched
    "campaign_records": {
        "impressions": "INTEGER DEFAULT 0",
        "clicks": "INTEGER DEFAULT 0",
        "conversions": "INTEGER DEFAULT 0",
        "cpm": "REAL DEFAULT 0",
        "cpc": "REAL DEFAULT 0",
        "cpa": "REAL DEFAULT 0",
        "ctr": "REAL DEFAULT 0",
        "launched_at": "DATETIME",
        "is_smart_plus": "BOOLEAN DEFAULT 0",
    },
    "creatives": {
        "archived": "BOOLEAN DEFAULT 0",
        "favorite": "BOOLEAN DEFAULT 0",
        "labels": "TEXT DEFAULT ''",
        "freshen": "BOOLEAN DEFAULT 0",
        "freshen_intensity": "TEXT DEFAULT ''",
        "freshen_mirror": "BOOLEAN DEFAULT 0",
        "src_path": "TEXT DEFAULT ''",
        "source_md5": "TEXT DEFAULT ''",
        "error": "TEXT DEFAULT ''",
        "tp_model_ids": "TEXT DEFAULT ''",
        "tp_video_id": "TEXT DEFAULT ''",
        "tp_job_id": "TEXT DEFAULT ''",
        "tp_cost": "REAL DEFAULT 0",
        "tp_checked_at": "DATETIME",
        "uniquify": "BOOLEAN DEFAULT 0",
        "kind": "TEXT DEFAULT 'video'",
        "ai_prompt": "TEXT DEFAULT ''",
        "ai_model": "TEXT DEFAULT ''",
        "ai_cost": "REAL DEFAULT 0",
        "carousel_images": "TEXT DEFAULT ''",
        "music_id": "TEXT DEFAULT ''",
        "music_name": "TEXT DEFAULT ''",
        "music_author": "TEXT DEFAULT ''",
        # text tool: editable text copies
        "text_spec": "TEXT DEFAULT ''",
        "text_parent_id": "INTEGER",
    },
    "creative_uploads": {
        "image_id": "TEXT DEFAULT ''",
        "image_url": "TEXT DEFAULT ''",
        "upload_md5": "TEXT DEFAULT ''",
    },
}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401 — register all models on Base

    Base.metadata.create_all(bind=engine)

    # Light migration: add any missing columns on existing tables.
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in schema_additions.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, sqltype in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}"))
        # Safety net: every column a model declares must exist on the live table,
        # listed above or not (a duplicate dict key once silently dropped an entry
        # and took the Campaigns page down with "no such column").
        for table_obj in Base.metadata.sorted_tables:
            if table_obj.name not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table_obj.name)}
            for column in table_obj.columns:
                if column.name in existing:
                    continue
                sqltype = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.default is not None and getattr(column.default, "is_scalar", False):
                    v = column.default.arg
                    default = " DEFAULT " + ("1" if v is True else "0" if v is False else repr(v) if isinstance(v, str) else str(v))
                conn.execute(text(f"ALTER TABLE {table_obj.name} ADD COLUMN {column.name} {sqltype}{default}"))


def missing_columns() -> dict[str, list[str]]:
    """{table: [column, …]} that the models declare but the live DB lacks — a
    check for tests and the Settings page ({} means the schema is complete)."""
    from . import models  # noqa: F401
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    out: dict[str, list[str]] = {}
    for table_obj in Base.metadata.sorted_tables:
        if table_obj.name not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table_obj.name)}
        gone = [c.name for c in table_obj.columns if c.name not in existing]
        if gone:
            out[table_obj.name] = gone
    return out
