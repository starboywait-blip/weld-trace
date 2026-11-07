"""Upgrade test: a database made by the first WeldTrace version must open, keep every record,
and accept the new item types and material/electrode fields.  Run: python tests/migration_test.py"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

data = Path(tempfile.mkdtemp())
os.environ["WELDTRACE_DATA"] = str(data)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OLD_SCHEMA = """
CREATE TABLE welders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, badge TEXT NOT NULL UNIQUE,
    qualification TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL CHECK (item_type IN ('tank','frame','gate','other')),
    customer TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE welds (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id),
    joint_label TEXT NOT NULL, process TEXT DEFAULT 'MIG', welder_id INTEGER NOT NULL REFERENCES welders(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (job_id, joint_label));
CREATE TABLE inspections (id INTEGER PRIMARY KEY AUTOINCREMENT, weld_id INTEGER NOT NULL REFERENCES welds(id),
    photo_file TEXT NOT NULL, photo_sha256 TEXT NOT NULL, captured_at TEXT, latitude REAL, longitude REAL,
    photo_quality TEXT NOT NULL, model_version TEXT NOT NULL, predicted_label TEXT, confidence REAL, probs_json TEXT,
    auto_decision TEXT NOT NULL CHECK (auto_decision IN ('pass','review','reject','retake')), inspector TEXT,
    final_label TEXT, final_decision TEXT CHECK (final_decision IN ('pass','reject') OR final_decision IS NULL),
    inspector_notes TEXT DEFAULT '', signed_off_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT (datetime('now')),
    actor TEXT, action TEXT NOT NULL, detail TEXT);
"""
con = sqlite3.connect(data / "weldtrace.db")
con.executescript(OLD_SCHEMA)
con.execute("INSERT INTO welders (name, badge) VALUES ('Amina K','W-001')")
con.execute("INSERT INTO jobs (ref, item_type, customer) VALUES ('TANK-OLD','tank','Acme')")
con.execute("INSERT INTO welds (job_id, joint_label, process, welder_id) VALUES (1,'seam-A','MIG',1)")
con.execute("INSERT INTO inspections (weld_id, photo_file, photo_sha256, photo_quality, model_version, auto_decision, "
            "inspector, final_label, final_decision, signed_off_at) VALUES (1,'a.jpg','abc','ok','no-model','review',"
            "'Insp1','good','pass','2026-10-12T10:00:00')")
con.commit()
con.close()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

with TestClient(app) as c:  # startup runs the upgrade
    jobs = c.get("/api/jobs").json()
    check([j["ref"] for j in jobs] == ["TANK-OLD"] and jobs[0]["status"] == "released", "old job kept, still released")
    rec = c.get(f"/api/jobs/{jobs[0]['id']}/record").json()
    w = rec["welds"][0]
    check(w["joint_label"] == "seam-A" and w["welder_name"] == "Amina K", "old weld and welder link kept")
    check(w["inspections"][0]["final_decision"] == "pass" and w["inspections"][0]["inspector"] == "Insp1",
          "old signed-off inspection kept")
    check(w["base_material"] == "" and w["electrode"] == "", "old joint has blank material/electrode (not invented)")
    check(c.get("/api/export.csv").text.count("not recorded") == 1, "CSV marks old joint 'not recorded'")

    new = c.post("/api/jobs", json={"ref": "STAIRS-1", "item_type": "stairs"})
    check(new.status_code == 201, "upgraded database accepts new item type (stairs)")
    ok = c.post("/api/welds", json={"job_id": new.json()["id"], "joint_label": "STRINGER-1", "welder_id": 1,
                                    "process": "Stick", "base_material": "mild_steel", "electrode": "E7018"})
    check(ok.status_code == 201, "can add a joint with material and electrode")
    check(c.post("/api/welds", json={"job_id": 1, "joint_label": "seam-B", "welder_id": 1, "process": "MIG",
                                     "base_material": "mild_steel", "electrode": "ER70S-6"}).status_code == 201,
          "can add a new joint to the old job")

# Running the upgrade a second time must be harmless.
with TestClient(app) as c:
    check(len(c.get("/api/jobs").json()) == 2, "second start: nothing lost or duplicated")
con = sqlite3.connect(data / "weldtrace.db")
check(con.execute("PRAGMA foreign_key_check").fetchall() == [], "no broken links between tables")
check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "database integrity ok")
print("\nUpgrade checks passed.")
