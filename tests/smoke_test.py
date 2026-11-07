"""End-to-end smoke test. Run from the project root:  python tests/smoke_test.py"""
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["WELDTRACE_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402  (only used to build a textured test image)
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.classifier import decide  # noqa: E402
from app.main import app  # noqa: E402


def jpeg(textured=True, size=(640, 480)):
    rng = np.random.default_rng(1)
    arr = (rng.integers(60, 200, (size[1], size[0], 3)) if textured else np.full((size[1], size[0], 3), 120)).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG")
    return buf.getvalue()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


with TestClient(app) as c:
    h = c.get("/api/health").json()
    check(h["ok"] and h["model_loaded"] is False, "health reports no-model mode")

    w1 = c.post("/api/welders", json={"name": "Amina K", "badge": "W-001", "qualification": "ISO 9606-1"}).json()
    w2 = c.post("/api/welders", json={"name": "Peter O", "badge": "W-002"}).json()
    check(c.post("/api/welders", json={"name": "Dup", "badge": "W-001"}).status_code == 409, "duplicate badge refused")

    # New item types, e.g. a chair, are accepted; unknown ones are not.
    chair = c.post("/api/jobs", json={"ref": "CHAIR-001", "item_type": "chair"})
    check(chair.status_code == 201, "job can be a chair")
    check(c.post("/api/jobs", json={"ref": "X-1", "item_type": "spaceship"}).status_code == 422, "unknown item type refused")
    opts = c.get("/api/options").json()
    check({"chair", "stairs", "gate"} <= {t["value"] for t in opts["item_types"]}, "options list chair, stairs, gate")

    job = c.post("/api/jobs", json={"ref": "TANK-0042", "item_type": "tank", "customer": "Acme"}).json()
    base = {"job_id": job["id"], "process": "MIG", "base_material": "mild_steel", "electrode": "ER70S-6"}
    s1 = c.post("/api/welds", json={**base, "joint_label": "seam-A", "welder_id": w1["id"]}).json()
    s2 = c.post("/api/welds", json={**base, "joint_label": "seam-B", "welder_id": w2["id"]}).json()
    check(s1["warnings"] == [], "matching material and electrode: no warning")
    check(c.post("/api/welds", json={**base, "joint_label": "seam-A", "welder_id": w2["id"]}).status_code == 409,
          "duplicate joint label refused")

    # Material and electrode are required, and advisories never block saving.
    miss = {k: v for k, v in base.items() if k != "electrode"}
    check(c.post("/api/welds", json={**miss, "joint_label": "m1", "welder_id": w1["id"]}).status_code == 422,
          "electrode is required")
    check(c.post("/api/welds", json={**base, "joint_label": "m2", "welder_id": w1["id"], "process": "Laser"}).status_code == 422,
          "unknown process refused")
    check(c.post("/api/welds", json={**base, "joint_label": "m3", "welder_id": w1["id"], "electrode": "Other:"}).status_code == 422,
          "empty 'Other' electrode refused")
    rb = c.post("/api/welds", json={**base, "joint_label": "rebar-1", "welder_id": w1["id"],
                                    "process": "Stick", "base_material": "rebar", "electrode": "E6013"})
    check(rb.status_code == 201 and any("E7018" in w for w in rb.json()["warnings"]), "rebar + E6013 saved with a warning")
    bad = c.post("/api/welds", json={**base, "joint_label": "ss-1", "welder_id": w1["id"],
                                     "base_material": "stainless_304"})
    check(bad.status_code == 201 and bad.json()["warnings"], "stainless + steel wire saved with a warning")
    oth = c.post("/api/welds", json={**base, "joint_label": "odd-1", "welder_id": w1["id"],
                                     "base_material": "Other: brass", "electrode": "Other: bronze rod"})
    check(oth.status_code == 201 and oth.json()["warnings"] == [], "free-text material/electrode accepted")
    check(c.get("/api/compat", params={"material": "aluminium", "process": "Stick", "electrode": "E6013"}).json()["warnings"],
          "compat endpoint warns on aluminium + stick steel rod")
    check(c.post("/api/welds", json={"job_id": chair.json()["id"], "joint_label": "LEG-1", "welder_id": w1["id"],
                                     "process": "Stick", "base_material": "mild_tube", "electrode": "E6013"}).status_code == 201,
          "joint on the chair job")

    # Blurry/flat photo is bounced for retake and cannot be signed off.
    r = c.post("/api/inspections", data={"weld_id": s1["id"], "inspector": "Insp1"},
               files={"photo": ("a.jpg", jpeg(textured=False), "image/jpeg")}).json()
    check(r["auto_decision"] == "retake", "flat photo -> retake")
    check(c.post(f"/api/inspections/{r['id']}/signoff", json={"inspector": "Insp1", "final_label": "good",
          "final_decision": "pass"}).status_code == 409, "cannot sign off a retake")

    # Good photo, no model -> review, never auto-pass.
    r1 = c.post("/api/inspections", data={"weld_id": s1["id"], "inspector": "Insp1"},
                files={"photo": ("a.jpg", jpeg(), "image/jpeg")}).json()
    check(r1["auto_decision"] == "review" and r1["predicted_label"] is None, "no model -> manual review, no invented label")

    bad = c.post(f"/api/inspections/{r1['id']}/signoff",
                 json={"inspector": "Insp1", "final_label": "crack", "final_decision": "pass"})
    check(bad.status_code == 422, "cannot pass a cracked weld")
    ok = c.post(f"/api/inspections/{r1['id']}/signoff",
                json={"inspector": "Insp1", "final_label": "good", "final_decision": "pass"})
    check(ok.status_code == 200, "inspector sign-off pass")
    check(c.post(f"/api/inspections/{r1['id']}/signoff",
                 json={"inspector": "Insp1", "final_label": "good", "final_decision": "pass"}).status_code == 409,
          "second sign-off blocked (append-only)")

    r2 = c.post("/api/inspections", data={"weld_id": s2["id"], "inspector": "Insp1"},
                files={"photo": ("b.jpg", jpeg(), "image/jpeg")}).json()
    c.post(f"/api/inspections/{r2['id']}/signoff",
           json={"inspector": "Insp1", "final_label": "porosity", "final_decision": "reject", "notes": "cluster"})

    rec = c.get(f"/api/jobs/{job['id']}/record").json()
    check(rec["status"] == "hold", "job on hold when any weld rejected")
    by = {w["joint_label"]: w for w in rec["welds"]}
    check(by["seam-A"]["welder_name"] == "Amina K" and by["seam-B"]["welder_name"] == "Peter O",
          "record shows who welded what")
    check(by["seam-A"]["base_material"] == "mild_steel" and by["seam-A"]["electrode"] == "ER70S-6",
          "record shows material and electrode")

    st = c.get(f"/api/welders/{w2['id']}/stats").json()
    check(st["reject_rate"] == 1.0 and st["defects"] == {"porosity": 1}, "welder stats")

    csv_text = c.get("/api/export.csv").text
    check("TANK-0042" in csv_text and "Peter O" in csv_text and "porosity" in csv_text, "CSV export")
    check("base_material" in csv_text and "Mild steel" in csv_text and "ER70S-6" in csv_text, "CSV has material and electrode")
    check(c.get(f"/api/photos/{rec['welds'][0]['inspections'][0]['photo_file']}").status_code == 200, "photo served")
    check(c.get("/api/photos/..%2Fweldtrace.db").status_code == 404, "path traversal blocked")

# Triage logic with a (simulated) model output.
check(decide("good", 0.97, {"good": 0.97, "crack": 0.01}) == "pass", "confident good -> pass")
check(decide("good", 0.92, {"good": 0.92, "crack": 0.25}) == "review", "good but crack-suspicious -> review")
check(decide("good", 0.70, {"good": 0.70}) == "review", "low-confidence good -> review")
check(decide("crack", 0.85, {"crack": 0.85}) == "reject", "confident crack -> reject")
check(decide("crack", 0.40, {"crack": 0.40}) == "review", "unsure crack -> review, never pass")
check(decide("spatter", 0.30, {"spatter": 0.30}) == "review", "unsure minor defect -> review")
print("\nAll checks passed.")
