"""Weld defect classifier wrapper.

Loads a fine-tuned image classifier if weights exist (data/model/weld_model.pt plus labels.json).
If not, the system runs in *no-model* mode: nothing is auto-classified and every photo goes to a
human inspector. It never invents a prediction.
"""
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

MODEL_DIR = Path(os.environ.get("WELDTRACE_MODEL_DIR", Path(__file__).resolve().parent.parent / "data" / "model"))

GOOD_LABEL = "good"
# Defects that must never be auto-passed, even at low confidence.
CRITICAL = {"crack", "lack_of_fusion", "burn_through"}

PASS_CONF = float(os.environ.get("WELDTRACE_PASS_CONF", 0.90))      # min confidence to auto-pass 'good'
REJECT_CONF = float(os.environ.get("WELDTRACE_REJECT_CONF", 0.60))  # min confidence to auto-reject a defect
CRITICAL_FLAG = float(os.environ.get("WELDTRACE_CRITICAL_FLAG", 0.20))  # any critical prob above this blocks auto-pass


@dataclass
class PhotoQuality:
    ok: bool
    reason: str
    sharpness: float
    brightness: float


@dataclass
class Result:
    model_version: str
    label: str | None
    confidence: float | None
    probs: dict = field(default_factory=dict)
    decision: str = "review"  # pass | review | reject | retake


def check_photo_quality(img: Image.Image) -> PhotoQuality:
    """Cheap gate so blurry, dark or tiny photos are retaken instead of classified."""
    w, h = img.size
    gray = img.convert("L")
    small = gray.resize((min(w, 640), int(h * min(w, 640) / w))) if w > 640 else gray
    edges = small.filter(ImageFilter.FIND_EDGES)
    ew, eh = edges.size
    # PIL's edge filter leaves artefacts on the 1px border; measure the interior only.
    sharp = ImageStat.Stat(edges.crop((2, 2, ew - 2, eh - 2))).var[0]
    bright = ImageStat.Stat(small).mean[0]
    if min(w, h) < 224:
        return PhotoQuality(False, "Image too small (min 224px side). Move closer.", sharp, bright)
    if sharp < 40:
        return PhotoQuality(False, "Image looks blurry. Hold steady and refocus.", sharp, bright)
    if bright < 35:
        return PhotoQuality(False, "Too dark. Add light.", sharp, bright)
    if bright > 225:
        return PhotoQuality(False, "Overexposed. Reduce glare.", sharp, bright)
    return PhotoQuality(True, "ok", sharp, bright)


class WeldClassifier:
    def __init__(self):
        self.model = None
        self.labels: list[str] = []
        self.version = "no-model"
        self._load()

    @property
    def available(self) -> bool:
        return self.model is not None

    def _load(self):
        weights, labels = MODEL_DIR / "weld_model.pt", MODEL_DIR / "labels.json"
        if not (weights.exists() and labels.exists()):
            return
        try:
            import torch
            from torchvision import models, transforms

            meta = json.loads(labels.read_text())
            self.labels = meta["labels"]
            self.version = meta.get("version", "unversioned")
            net = models.mobilenet_v3_small(weights=None)
            net.classifier[-1] = torch.nn.Linear(net.classifier[-1].in_features, len(self.labels))
            net.load_state_dict(torch.load(weights, map_location="cpu"))
            net.eval()
            self.model = net
            self._torch = torch
            self._tf = transforms.Compose([
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ])
        except Exception as exc:  # missing torch, bad weights etc: degrade safely
            print(f"[classifier] could not load model, running in no-model mode: {exc}")
            self.model = None
            self.version = "no-model"

    def classify(self, image_bytes: bytes) -> tuple[Result, PhotoQuality]:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        pq = check_photo_quality(img)
        if not pq.ok:
            return Result(self.version, None, None, {}, "retake"), pq
        if not self.available:
            return Result(self.version, None, None, {}, "review"), pq

        torch = self._torch
        with torch.no_grad():
            x = self._tf(img).unsqueeze(0)
            p = torch.softmax(self.model(x), dim=1)[0].tolist()
        probs = {lbl: round(v, 4) for lbl, v in zip(self.labels, p)}
        label = max(probs, key=probs.get)
        conf = probs[label]
        return Result(self.version, label, conf, probs, decide(label, conf, probs)), pq


def decide(label: str, conf: float, probs: dict) -> str:
    """Conservative triage. Auto-pass is the hardest outcome to earn."""
    crit_max = max((probs.get(c, 0.0) for c in CRITICAL), default=0.0)
    if label == GOOD_LABEL:
        if conf >= PASS_CONF and crit_max < CRITICAL_FLAG:
            return "pass"
        return "review"
    if conf >= REJECT_CONF or label in CRITICAL:
        return "reject" if conf >= REJECT_CONF else "review"
    return "review"
