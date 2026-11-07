# WeldTrace

Photo-based weld inspection with a permanent record of **who welded what**, for tanks, frames and gates.

## How it works

1. A supervisor creates a **job** (e.g. `TANK-0042`), registers **welders** (name, badge, qualification), and registers each **joint** against the welder who made it.
2. An inspector opens the installable web app (PWA) on a phone, picks the joint, and photographs the weld.
3. The server checks photo quality (blur, light, size). Bad photos are bounced for a retake.
4. A vision model suggests a label (`good, porosity, undercut, crack, lack_of_fusion, overlap, spatter, burn_through`) and the system triages it to `pass`, `review`, `reject` or `retake`.
5. **The inspector signs off** with a final label and PASS/REJECT. The suggestion never releases a weld by itself.
6. Records are append-only: photo (SHA-256 hashed), model version, model output, inspector, timestamp, GPS if allowed, and an audit log. A job is `released` only when every joint has a signed-off pass, and goes on `hold` if any joint is rejected.

Safety rules built in: cracks, lack of fusion and burn-through can never be passed; low-confidence results always go to a human; with no trained model every photo goes to manual review (the system never invents a label).

## Item, material and electrode

- **Item type** (per job): tank, frame, gate, chair / stool, table / bench, stairs / ladder, handrail, window grille, door, shelf / rack, other.
- **Per joint** you record the **material being welded** (mild steel, tube, rebar, galvanized, stainless 304/316, aluminium, cast iron, spring steel, or "Other: your own text"), the **process** (Stick, MIG, TIG, Flux-core) and the **electrode / filler**. The electrode list changes with the process, with "Other" for anything not listed.
- The app shows **advisory warnings** for risky combinations (for example rebar with E6013, stainless with a mild-steel rod, aluminium with Stick, galvanized fumes). They never block saving; the welder or inspector decides. The rules live in `app/catalog.py` and are easy to edit.
- Material and electrode appear in the job's quality record and in the CSV export.

## Upgrading from an earlier version

Stop the server, copy the new files over the old folder (do **not** delete the `data` folder), and start it again. The database upgrades itself on start and keeps every record. Joints entered before the upgrade show material and electrode as "not recorded". On phones, close and reopen the app (or refresh once) to load the new screens.

## Run

```bash
pip install -r requirements.txt
export WELDTRACE_API_KEY=choose-a-secret      # recommended; omit only for local testing
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<server-ip>:8000` on the phone, go to **Settings**, enter your name and the API key. To install on the phone, use "Add to Home screen". Note: phone cameras and install prompts require **HTTPS** outside localhost, so put it behind a TLS proxy (Caddy, nginx) for real use.

Tests: `python tests/smoke_test.py` and `python tests/migration_test.py`

## Train the defect model with your labelled photos

Arrange photos in folders named by class:

```
dataset/good/*.jpg   dataset/porosity/*.jpg   dataset/crack/*.jpg ...
```

```bash
python train/train.py --data dataset --epochs 15
```

This fine-tunes MobileNetV3-small (needs internet once for the pretrained ImageNet weights), writes `data/model/weld_model.pt` + `labels.json`, and prints per-class recall. Restart the server to load it. The health endpoint and Settings screen show the loaded model version, and every inspection records which model version produced its suggestion.

**Before trusting it:** judge it by recall on `crack`, `lack_of_fusion` and `burn_through`, not overall accuracy. Validate on photos from your own workshop, phones and lighting that were not used in training. Keep the human sign-off until you have months of agreement data. Adjust triage thresholds with `WELDTRACE_PASS_CONF` (0.90), `WELDTRACE_REJECT_CONF` (0.60), `WELDTRACE_CRITICAL_FLAG` (0.20).

## Limits to be aware of

- A surface photo cannot see internal defects (internal porosity, lack of penetration, inclusions). For pressure-bearing tanks keep required NDT (dye penetrant, UT, radiography) per your code or customer spec; this system records visual inspection only.
- The photo quality thresholds (sharpness 40, brightness 35-225) are starting values tuned on synthetic images. Tune them on real weld photos.
- Auth is a single shared API key plus inspector name. For individual accountability add per-user logins.
- SQLite and local photo storage suit one workshop. Back up `data/` regularly.

## Layout

```
app/        FastAPI backend (main.py, db.py, classifier.py)
web/        Mobile PWA (offline capture queue, sign-off, records)
train/      Fine-tuning script
tests/      End-to-end smoke test
data/       Database, photos, model weights (created at runtime)
```
