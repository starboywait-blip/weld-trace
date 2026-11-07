"""Turn a Roboflow "YOLO" download into class folders for training, without the usual traps.

Roboflow exports often contain several augmented copies (flipped, blurred...) of every source
photo. If those copies are spread over the training and test groups the model is "tested" on
photos it has already seen, and the scores look better than they are. This script keeps ONE copy
per source photo by default.

Images are copied into a STAGING folder (not your real dataset) so you can look at them, delete
the bad ones, and then move the good ones into your dataset yourself.

Example (PowerShell, from your project folder):
    python tools\\import_yolo.py --src "C:\\Downloads\\Crack.v1i.yolov8" ^
        --out "C:\\weldtrace2\\tracer\\dataset_incoming" --map crack=crack

--map NAME=TARGET   dataset class NAME (from the download's data.yaml) goes into folder TARGET.
                    Repeat it for several classes. Classes that are not mapped are ignored, and
                    images containing only ignored classes are skipped.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
SPLITS = ("train", "valid", "val", "test")


def read_names(yaml_path: Path) -> list[str]:
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^names:\s*\[(.*?)\]", text, re.M | re.S)
    if m:
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
    names: dict[int, str] = {}
    in_block, seq = False, []
    for line in text.splitlines():
        if re.match(r"^names:\s*$", line):
            in_block = True
            continue
        if in_block:
            d = re.match(r"^\s+(\d+):\s*(.+)$", line)
            s = re.match(r"^\s+-\s*(.+)$", line)
            if d:
                names[int(d.group(1))] = d.group(2).strip().strip("'\"")
            elif s:
                seq.append(s.group(1).strip().strip("'\""))
            elif line.strip() and not line.startswith((" ", "\t")):
                break
    return [names[i] for i in sorted(names)] if names else seq


def safe_name(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:80]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="the extracted Roboflow YOLO folder (contains data.yaml)")
    ap.add_argument("--out", required=True, help="staging folder to create class folders in")
    ap.add_argument("--map", action="append", required=True, metavar="NAME=TARGET")
    ap.add_argument("--keep-copies", action="store_true",
                    help="keep every augmented copy (NOT recommended: inflates test scores)")
    a = ap.parse_args()

    src, out = Path(a.src), Path(a.out)
    if not (src / "data.yaml").exists():
        sys.exit(f"data.yaml not found in {src}. Point --src at the extracted download folder.")
    names = read_names(src / "data.yaml")
    mapping = {}
    for item in a.map:
        if "=" not in item:
            sys.exit(f"--map needs NAME=TARGET, got: {item}")
        k, v = item.split("=", 1)
        if k.strip() not in names:
            sys.exit(f"Class '{k}' is not in data.yaml. Classes there: {names}")
        mapping[names.index(k.strip())] = v.strip()
    print("classes in download:", names)
    print("mapping:", {names[i]: t for i, t in mapping.items()})

    seen, copied, stats = set(), {}, {"images": 0, "dup_copies": 0, "unmapped_only": 0,
                                      "mixed_targets": 0, "no_label": 0, "already_there": 0}
    for split in SPLITS:
        img_dir, lab_dir = src / split / "images", src / split / "labels"
        if not img_dir.is_dir():
            continue
        for img in sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMG_EXT):
            stats["images"] += 1
            lab = lab_dir / (img.stem + ".txt")
            if not lab.exists():
                stats["no_label"] += 1
                continue
            ids = set()
            for line in lab.read_text().splitlines():
                tok = line.split()
                if tok and tok[0].isdigit():
                    ids.add(int(tok[0]))
            targets = {mapping[i] for i in ids if i in mapping}
            if not targets:
                stats["unmapped_only"] += 1
                continue
            if len(targets) > 1:
                stats["mixed_targets"] += 1  # one photo showing two kinds of defect: too ambiguous
                continue
            target = targets.pop()
            key = img.name.split(".rf.")[0]
            if not a.keep_copies:
                if key in seen:
                    stats["dup_copies"] += 1
                    continue
                seen.add(key)
            dest_dir = out / target
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"pub_{safe_name(key if not a.keep_copies else img.stem)}{img.suffix.lower()}"
            if dest.exists() and dest.stat().st_size == img.stat().st_size:
                stats["already_there"] += 1  # imported on an earlier run: leave it alone
                continue
            n = 1
            while dest.exists():  # a different photo with the same short name
                dest = dest_dir / f"pub_{safe_name(key)}_{n}{img.suffix.lower()}"
                n += 1
            shutil.copy2(img, dest)
            copied[target] = copied.get(target, 0) + 1

    print("\nimages found:", stats["images"])
    print("skipped as extra copies of the same photo:", stats["dup_copies"])
    print("skipped (only classes you did not map):", stats["unmapped_only"])
    print("skipped (two different target classes in one photo):", stats["mixed_targets"])
    print("skipped (no label file):", stats["no_label"])
    print("skipped (already imported earlier):", stats["already_there"])
    print("\ncopied into", out)
    for t, n in sorted(copied.items()):
        print(f"  {t}: {n} photos")
    print("\nNext: open the folder, delete any photo that is not a clear example, "
          "then move the rest into your dataset folder.")


if __name__ == "__main__":
    main()
