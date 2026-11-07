"""SQLite storage for WeldTrace. Append-only inspection history with an audit log."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("WELDTRACE_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "weldtrace.db"
PHOTO_DIR = DATA_DIR / "photos"

SCHEMA = """
CREATE TABLE IF NOT EXISTS welders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    badge TEXT NOT NULL UNIQUE,
    qualification TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    customer TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS welds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    joint_label TEXT NOT NULL,
    process TEXT DEFAULT 'MIG',
    base_material TEXT NOT NULL DEFAULT '',
    electrode TEXT NOT NULL DEFAULT '',
    welder_id INTEGER NOT NULL REFERENCES welders(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, joint_label)
);
CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weld_id INTEGER NOT NULL REFERENCES welds(id),
    photo_file TEXT NOT NULL,
    photo_sha256 TEXT NOT NULL,
    captured_at TEXT,
    latitude REAL,
    longitude REAL,
    photo_quality TEXT NOT NULL,
    model_version TEXT NOT NULL,
    predicted_label TEXT,
    confidence REAL,
    probs_json TEXT,
    auto_decision TEXT NOT NULL CHECK (auto_decision IN ('pass','review','reject','retake')),
    inspector TEXT,
    final_label TEXT,
    final_decision TEXT CHECK (final_decision IN ('pass','reject') OR final_decision IS NULL),
    inspector_notes TEXT DEFAULT '',
    signed_off_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT (datetime('now')),
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_welds_welder ON welds(welder_id);
CREATE INDEX IF NOT EXISTS idx_insp_weld ON inspections(weld_id);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        migrate(c)


def migrate(c) -> None:
    """Upgrade databases created by earlier versions without losing data."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(welds)")}
    if "base_material" not in cols:
        c.execute("ALTER TABLE welds ADD COLUMN base_material TEXT NOT NULL DEFAULT ''")
    if "electrode" not in cols:
        c.execute("ALTER TABLE welds ADD COLUMN electrode TEXT NOT NULL DEFAULT ''")

    # Older versions only allowed tank/frame/gate/other. Rebuild the jobs table without that limit.
    sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()["sql"]
    if "CHECK" in sql.upper():
        c.commit()
        c.executescript("""
            PRAGMA foreign_keys = OFF;
            BEGIN;
            CREATE TABLE jobs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref TEXT NOT NULL UNIQUE,
                item_type TEXT NOT NULL,
                customer TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO jobs_new (id, ref, item_type, customer, notes, created_at)
                SELECT id, ref, item_type, customer, notes, created_at FROM jobs;
            DROP TABLE jobs;
            ALTER TABLE jobs_new RENAME TO jobs;
            COMMIT;
            PRAGMA foreign_keys = ON;
        """)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def audit(conn, actor: str, action: str, detail: str = "") -> None:
    conn.execute("INSERT INTO audit_log (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))
