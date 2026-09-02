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
    connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# table -> {column: SQL type} — add new columns here when models grow.
# Everything currently in models.py is covered by create_all; entries below
# exist so FUTURE columns can be added without wiping the live DB.
schema_additions: dict[str, dict[str, str]] = {
    # example: "templates": {"campaign_name_pattern": "TEXT DEFAULT ''"},
    "templates": {"campaign_name_pattern": "TEXT DEFAULT ''"},
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
    },
    # Phase 2 (sources/P&L): new columns on live DBs
    "launch_logs": {
        "spark_code_id": "INTEGER",
        "source": "TEXT DEFAULT ''",
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
    with engine.begin() as conn:
        for table, cols in schema_additions.items():
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, sqltype in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}"))
