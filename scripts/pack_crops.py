"""
Packs a sample set's crops/ folder into one crops.zip beside its jsonl files (stored, not compressed: PNGs are already compressed).

  python scripts/pack_crops.py data/real_awb/work/trainset

The GPU box caps an account at 200k files and a set holds thousands of small PNGs; scripts/gpu/prompts.py:open_crop reads the archive.
"""

import sys
import zipfile
from pathlib import Path

root = Path(sys.argv[1])
crops = root / "crops"
with zipfile.ZipFile(root / "crops.zip", "w", zipfile.ZIP_STORED) as z:
    n = 0
    for p in sorted(crops.rglob("*.png")):
        z.write(p, p.relative_to(crops).as_posix())
        n += 1
print(f"{n} crops -> {root / 'crops.zip'}  ({(root / 'crops.zip').stat().st_size / 1e6:.0f} MB)")
