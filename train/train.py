"""Fine-tune a MobileNetV3-small weld defect classifier.

Dataset layout (folder name = class label):
    dataset/good/*.jpg
    dataset/porosity/*.jpg
    dataset/crack/*.jpg ...

Usage:
    python train/train.py --data dataset --epochs 15
Outputs data/model/weld_model.pt and data/model/labels.json, plus a per-class report.
Judge the model on RECALL of crack / lack_of_fusion / burn_through, not overall accuracy.
"""
import argparse
import json
import random
from datetime import date
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader, Subset
from PIL import Image, ImageFile
from torchvision import datasets, models, transforms

ImageFile.LOAD_TRUNCATED_IMAGES = True  # accept slightly cut-off files instead of crashing

CRITICAL = {"crack", "lack_of_fusion", "burn_through"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "model"))
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    random.seed(a.seed)
    torch.manual_seed(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    norm = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    train_tf = transforms.Compose([
        transforms.RandomResizedCrop(224, scale=(0.7, 1.0)),
        transforms.RandomHorizontalFlip(), transforms.RandomVerticalFlip(),
        transforms.ColorJitter(0.3, 0.3, 0.2), transforms.ToTensor(), norm])
    val_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(), norm])

    full_train = datasets.ImageFolder(a.data, transform=train_tf)
    full_val = datasets.ImageFolder(a.data, transform=val_tf)
    labels = full_train.classes

    # Drop any file that PIL cannot open (HEIC renamed to .jpg, damaged copies, ...) and say which.
    good, skipped = [], []
    for path, cls in full_train.samples:
        try:
            with Image.open(path) as im:
                im.convert("RGB").load()
            good.append((path, cls))
        except Exception as exc:
            skipped.append((path, str(exc)[:80]))
    for path, why in skipped:
        print("SKIPPED unreadable image:", path, "-", why)
    if not good:
        raise SystemExit("No readable images found in " + a.data)
    for ds in (full_train, full_val):
        ds.samples = list(good)
        ds.imgs = list(good)
        ds.targets = [c for _, c in good]
    print("classes:", labels, "images:", len(full_train), "skipped:", len(skipped))

    # Stratified split so rare defect classes appear in validation.
    by_class: dict[int, list[int]] = {}
    for i, (_, y) in enumerate(full_train.samples):
        by_class.setdefault(y, []).append(i)
    tr_idx, va_idx = [], []
    for idxs in by_class.values():
        random.shuffle(idxs)
        k = max(1, int(len(idxs) * a.val_frac))
        va_idx += idxs[:k]
        tr_idx += idxs[k:]

    train_dl = DataLoader(Subset(full_train, tr_idx), batch_size=a.batch, shuffle=True, num_workers=2)
    val_dl = DataLoader(Subset(full_val, va_idx), batch_size=a.batch, num_workers=2)

    counts = torch.tensor([len([i for i in tr_idx if full_train.samples[i][1] == c]) for c in range(len(labels))],
                          dtype=torch.float)
    weights = (counts.sum() / (len(labels) * counts.clamp(min=1))).to(dev)  # up-weight rare defects

    net = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    net.classifier[-1] = nn.Linear(net.classifier[-1].in_features, len(labels))
    net.to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.epochs)
    loss_fn = nn.CrossEntropyLoss(weight=weights)

    best, best_state = -1.0, None
    for ep in range(a.epochs):
        net.train()
        for x, y in train_dl:
            x, y = x.to(dev), y.to(dev)
            opt.zero_grad()
            loss_fn(net(x), y).backward()
            opt.step()
        sched.step()
        recall, acc = evaluate(net, val_dl, labels, dev)
        crit = [recall[l] for l in labels if l in CRITICAL and l in recall]
        score = (sum(crit) / len(crit) if crit else acc) * 0.6 + acc * 0.4  # favour catching critical defects
        print(f"epoch {ep + 1}/{a.epochs} acc={acc:.3f} score={score:.3f}")
        if score > best:
            best, best_state = score, {k: v.cpu().clone() for k, v in net.state_dict().items()}

    net.load_state_dict(best_state)
    recall, acc = evaluate(net, val_dl, labels, dev)
    print("\nPer-class recall on validation set:")
    for l in labels:
        flag = "  <-- critical" if l in CRITICAL else ""
        print(f"  {l:16s} {recall.get(l, float('nan')):.3f}{flag}")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(best_state, out / "weld_model.pt")
    version = f"mnv3s-{date.today().isoformat()}"
    (out / "labels.json").write_text(json.dumps({"labels": labels, "version": version,
                                                 "val_accuracy": round(acc, 4),
                                                 "val_recall": {k: round(v, 4) for k, v in recall.items()},
                                                 "n_train": len(tr_idx), "n_val": len(va_idx)}, indent=2))
    print(f"\nSaved model {version} to {out}")


@torch.no_grad()
def evaluate(net, dl, labels, dev):
    net.eval()
    hit = [0] * len(labels)
    tot = [0] * len(labels)
    for x, y in dl:
        pred = net(x.to(dev)).argmax(1).cpu()
        for p, t in zip(pred.tolist(), y.tolist()):
            tot[t] += 1
            hit[t] += int(p == t)
    recall = {labels[i]: hit[i] / tot[i] for i in range(len(labels)) if tot[i]}
    acc = sum(hit) / max(1, sum(tot))
    return recall, acc


if __name__ == "__main__":
    main()
