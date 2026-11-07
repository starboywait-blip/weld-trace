"""WeldTrace API: weld photo inspection with per-weld, per-welder quality records."""
import csv
import hashlib
import io
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, db
from .classifier import CRITICAL, WeldClassifier

DEFECTS = ["good", "porosity", "undercut", "crack", "lack_of_fusion", "overlap", "spatter", "burn_through"]
API_KEY = os.environ.get("WELDTRACE_API_KEY")  # unset = open (dev only)
MAX_PHOTO_BYTES = 12 * 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="WeldTrace", version="0.1.0")
classifier = WeldClassifier()


@app.on_event("startup")
def _startup():
    db.init_db()


def require_key(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and not (x_api_key and secrets.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(401, "Invalid or missing API key")


auth = [Depends(require_key)]


# ---------- models ----------
class WelderIn(BaseModel):
    name: str = Field(min_length=1)
    badge: str = Field(min_length=1)
    qualification: str = ""


class JobIn(BaseModel):
    ref: str = Field(min_length=1)
    item_type: str
    customer: str = ""
    notes: str = ""


class WeldIn(BaseModel):
    job_id: int
    joint_label: str = Field(min_length=1)
    welder_id: int
    process: str
    base_material: str = Field(min_length=1, max_length=80)
    electrode: str = Field(min_length=1, max_length=80)


class SignOff(BaseModel):
    inspector: str = Field(min_length=1)
    final_label: str
    final_decision: str = Field(pattern="^(pass|reject)$")
    notes: str = ""


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- basics ----------
@app.get("/api/health")
def health():
    return {"ok": True, "model_version": classifier.version, "model_loaded": classifier.available,
            "classes": DEFECTS}


@app.get("/api/options", dependencies=auth)
def get_options():
    return catalog.options()


@app.get("/api/compat", dependencies=auth)
def compat(material: str, process: str, electrode: str):
    return {"warnings": catalog.check_compat(material, process, electrode)}


@app.post("/api/welders", dependencies=auth, status_code=201)
def add_welder(w: WelderIn):
    with db.connect() as c:
        try:
            cur = c.execute("INSERT INTO welders (name, badge, qualification) VALUES (?,?,?)",
                            (w.name, w.badge, w.qualification))
        except Exception:
            raise HTTPException(409, "Badge already exists")
        db.audit(c, "system", "welder_added", w.badge)
        return {"id": cur.lastrowid, **w.model_dump()}


@app.get("/api/welders", dependencies=auth)
def list_welders():
    with db.connect() as c:
        return rows(c.execute("SELECT * FROM welders WHERE active=1 ORDER BY name"))


@app.post("/api/jobs", dependencies=auth, status_code=201)
def add_job(j: JobIn):
    if j.item_type not in catalog.ITEM_KEYS:
        raise HTTPException(422, f"item_type must be one of {sorted(catalog.ITEM_KEYS)}")
    with db.connect() as c:
        try:
            cur = c.execute("INSERT INTO jobs (ref, item_type, customer, notes) VALUES (?,?,?,?)",
                            (j.ref, j.item_type, j.customer, j.notes))
        except Exception:
            raise HTTPException(409, "Job ref already exists")
        db.audit(c, "system", "job_added", j.ref)
        return {"id": cur.lastrowid, **j.model_dump()}


@app.get("/api/jobs", dependencies=auth)
def list_jobs():
    with db.connect() as c:
        jobs = rows(c.execute("SELECT * FROM jobs ORDER BY id DESC"))
        for j in jobs:
            j["status"] = _job_status(c, j["id"])
        return jobs


@app.post("/api/welds", dependencies=auth, status_code=201)
def add_weld(w: WeldIn):
    if w.process not in catalog.PROCESSES:
        raise HTTPException(422, f"process must be one of {catalog.PROCESSES}")
    for field, val in (("base_material", w.base_material), ("electrode", w.electrode)):
        if val.startswith("Other:") and not val[6:].strip():
            raise HTTPException(422, f"Describe the {field.replace('_', ' ')} when choosing Other")
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM jobs WHERE id=?", (w.job_id,)).fetchone():
            raise HTTPException(404, "Job not found")
        if not c.execute("SELECT 1 FROM welders WHERE id=? AND active=1", (w.welder_id,)).fetchone():
            raise HTTPException(404, "Welder not found")
        try:
            cur = c.execute("INSERT INTO welds (job_id, joint_label, process, base_material, electrode, welder_id) "
                            "VALUES (?,?,?,?,?,?)",
                            (w.job_id, w.joint_label, w.process, w.base_material, w.electrode, w.welder_id))
        except Exception:
            raise HTTPException(409, "Joint label already used on this job")
        db.audit(c, "system", "weld_added",
                 f"job={w.job_id} joint={w.joint_label} welder={w.welder_id} "
                 f"material={w.base_material} process={w.process} electrode={w.electrode}")
        return {"id": cur.lastrowid, **w.model_dump(),
                "warnings": catalog.check_compat(w.base_material, w.process, w.electrode)}


@app.get("/api/welds", dependencies=auth)
def list_welds(job_id: int):
    with db.connect() as c:
        out = rows(c.execute(
            "SELECT w.*, wr.name AS welder_name, wr.badge FROM welds w "
            "JOIN welders wr ON wr.id=w.welder_id WHERE w.job_id=? ORDER BY w.id", (job_id,)))
        for w in out:
            w["status"] = _weld_status(c, w["id"])
        return out


# ---------- inspections ----------
@app.post("/api/inspections", dependencies=auth, status_code=201)
async def create_inspection(
    weld_id: int = Form(...),
    photo: UploadFile = File(...),
    inspector: str = Form(""),
    captured_at: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
):
    data = await photo.read()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo empty or larger than 12 MB")
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM welds WHERE id=?", (weld_id,)).fetchone():
            raise HTTPException(404, "Weld not found")
    try:
        result, pq = classifier.classify(data)
    except Exception:
        raise HTTPException(400, "Could not read image")

    sha = hashlib.sha256(data).hexdigest()
    fname = f"{sha[:16]}_{int(datetime.now(timezone.utc).timestamp())}.jpg"
    (db.PHOTO_DIR / fname).write_bytes(data)

    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO inspections (weld_id, photo_file, photo_sha256, captured_at, latitude, longitude, "
            "photo_quality, model_version, predicted_label, confidence, probs_json, auto_decision, inspector) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (weld_id, fname, sha, captured_at, latitude, longitude, pq.reason, result.model_version,
             result.label, result.confidence, json.dumps(result.probs), result.decision, inspector))
        db.audit(c, inspector or "unknown", "inspection_created", f"id={cur.lastrowid} weld={weld_id} auto={result.decision}")
        return {"id": cur.lastrowid, "auto_decision": result.decision, "predicted_label": result.label,
                "confidence": result.confidence, "probs": result.probs, "photo_quality": pq.reason,
                "model_version": result.model_version,
                "needs_signoff": result.decision != "retake"}


@app.post("/api/inspections/{insp_id}/signoff", dependencies=auth)
def sign_off(insp_id: int, s: SignOff):
    if s.final_label not in DEFECTS:
        raise HTTPException(422, f"final_label must be one of {DEFECTS}")
    if s.final_decision == "pass" and s.final_label != "good":
        raise HTTPException(422, "Cannot pass a weld labelled with a defect")
    if s.final_decision == "pass" and s.final_label in CRITICAL:
        raise HTTPException(422, "Critical defects cannot be passed")
    with db.connect() as c:
        r = c.execute("SELECT * FROM inspections WHERE id=?", (insp_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Inspection not found")
        if r["auto_decision"] == "retake":
            raise HTTPException(409, "Photo was rejected for quality; capture a new one")
        if r["signed_off_at"]:
            raise HTTPException(409, "Already signed off; capture a new inspection to revise")
        c.execute("UPDATE inspections SET final_label=?, final_decision=?, inspector=?, inspector_notes=?, "
                  "signed_off_at=? WHERE id=?",
                  (s.final_label, s.final_decision, s.inspector, s.notes,
                   datetime.now(timezone.utc).isoformat(timespec="seconds"), insp_id))
        db.audit(c, s.inspector, "signoff",
                 f"id={insp_id} model={r['predicted_label']} final={s.final_label}/{s.final_decision}")
        return {"id": insp_id, "final_label": s.final_label, "final_decision": s.final_decision}


@app.get("/api/photos/{name}", dependencies=auth)
def photo(name: str):
    p = (db.PHOTO_DIR / name).resolve()
    if p.parent != db.PHOTO_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


# ---------- records & reports ----------
def _weld_status(c, weld_id: int) -> str:
    r = c.execute("SELECT final_decision, auto_decision FROM inspections WHERE weld_id=? "
                  "ORDER BY id DESC LIMIT 1", (weld_id,)).fetchone()
    if not r:
        return "uninspected"
    if r["final_decision"]:
        return r["final_decision"]
    return "retake" if r["auto_decision"] == "retake" else "awaiting_signoff"


def _job_status(c, job_id: int) -> str:
    ids = [r["id"] for r in c.execute("SELECT id FROM welds WHERE job_id=?", (job_id,))]
    if not ids:
        return "no_welds"
    st = [_weld_status(c, i) for i in ids]
    if "reject" in st:
        return "hold"
    return "released" if all(s == "pass" for s in st) else "in_progress"


@app.get("/api/jobs/{job_id}/record", dependencies=auth)
def job_record(job_id: int):
    with db.connect() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        welds = rows(c.execute(
            "SELECT w.*, wr.name AS welder_name, wr.badge, wr.qualification FROM welds w "
            "JOIN welders wr ON wr.id=w.welder_id WHERE w.job_id=? ORDER BY w.id", (job_id,)))
        for w in welds:
            w["status"] = _weld_status(c, w["id"])
            w["inspections"] = rows(c.execute(
                "SELECT id, captured_at, created_at, photo_file, photo_sha256, model_version, predicted_label, "
                "confidence, auto_decision, inspector, final_label, final_decision, inspector_notes, signed_off_at "
                "FROM inspections WHERE weld_id=? ORDER BY id", (w["id"],)))
        return {"job": dict(job), "status": _job_status(c, job_id), "welds": welds}


@app.get("/api/welders/{welder_id}/stats", dependencies=auth)
def welder_stats(welder_id: int):
    with db.connect() as c:
        w = c.execute("SELECT * FROM welders WHERE id=?", (welder_id,)).fetchone()
        if not w:
            raise HTTPException(404, "Welder not found")
        # Count each weld once, by its latest signed-off inspection.
        data = rows(c.execute(
            "SELECT i.final_label, i.final_decision FROM inspections i "
            "JOIN welds ON welds.id=i.weld_id WHERE welds.welder_id=? AND i.signed_off_at IS NOT NULL "
            "AND i.id = (SELECT MAX(id) FROM inspections x WHERE x.weld_id=i.weld_id AND x.signed_off_at IS NOT NULL)",
            (welder_id,)))
        total = len(data)
        rejected = sum(1 for d in data if d["final_decision"] == "reject")
        by_defect: dict = {}
        for d in data:
            if d["final_label"] != "good":
                by_defect[d["final_label"]] = by_defect.get(d["final_label"], 0) + 1
        return {"welder": dict(w), "welds_assessed": total, "rejected": rejected,
                "reject_rate": round(rejected / total, 3) if total else None, "defects": by_defect}


@app.get("/api/export.csv", dependencies=auth)
def export_csv():
    with db.connect() as c:
        data = c.execute(
            "SELECT j.ref AS job, j.item_type, w.joint_label, w.base_material, w.process, w.electrode, "
            "wr.name AS welder, wr.badge, "
            "i.id AS inspection_id, i.created_at, i.photo_sha256, i.model_version, i.predicted_label, i.confidence, "
            "i.auto_decision, i.inspector, i.final_label, i.final_decision, i.signed_off_at, i.inspector_notes "
            "FROM inspections i JOIN welds w ON w.id=i.weld_id JOIN jobs j ON j.id=w.job_id "
            "JOIN welders wr ON wr.id=w.welder_id ORDER BY i.id").fetchall()
    buf = io.StringIO()
    wtr = csv.writer(buf)
    if data:
        wtr.writerow(data[0].keys())
        for r in data:
            d = dict(r)
            d["item_type"] = catalog.item_label(d["item_type"])
            d["base_material"] = catalog.material_label(d["base_material"]) or "not recorded"
            wtr.writerow(list(d.values()))
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=weld_records.csv"})


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
