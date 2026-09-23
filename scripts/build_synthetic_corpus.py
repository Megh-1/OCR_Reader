#!/usr/bin/env python
"""
Builds a labeled-SYNTHETIC evaluation corpus from ONE real air waybill PDF.

This exists so every input tier has samples to run scripts/evaluate_accuracy.py
against. It is NOT a substitute for real documents, and every file it writes
says so in its name:

    reference__*    the real, unmodified source PDF (the control - same document,
                    no degradation, so what changes below is the degradation alone)
    synthetic__*    a degraded rendering of that same page

What is simulated, and what is not:
  tier 2 (scanned PDF)   image-only PDFs at several resolutions with skew, noise,
                         JPEG compression, fading, speckle and a bilevel scan, plus
                         a scan carrying an invisible OCR text overlay.
  tier 3 (camera JPEG)   the page composited onto a desk surface with rotation,
                         perspective, dim light, blur, a lighting gradient, sensor
                         noise and JPEG compression.
  NOT simulated          paper texture, impact-printer dithering, carbon-copy bleed,
                         folds, glare, a hand in the frame. A real photograph of a
                         printed waybill will be harder than anything here. Tier 1
                         (digital-born PDF) cannot be synthesized meaningfully: a
                         second copy of the same digital file measures nothing new.

Because every file is the same document, the ground truth is exact and shared:
the 29 values already verified in the ground-truth file, read at build time so
this script holds no document data.

    python scripts/build_synthetic_corpus.py
    python scripts/evaluate_accuracy.py --images-dir tests/fixtures/synthetic_awb \\
        --ground-truth tests/fixtures/synthetic_awb/ground_truth.json

Deterministic: fixed seeds, so a rebuild is byte-for-byte comparable run to run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402

PAGE_W, PAGE_H = 612, 792  # US Letter, in points


# --- page renderings ---------------------------------------------------------


def render_page(doc: "fitz.Document", dpi: int) -> np.ndarray:
    pix = doc[0].get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)


def encode(img: np.ndarray, jpeg_quality: int | None) -> tuple[bytes, str]:
    if jpeg_quality is None:
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes(), "png"
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return buf.tobytes(), "jpg"


def image_only_pdf(img: np.ndarray, jpeg_quality: int | None = 88, overlay_words=None) -> bytes:
    """A scanned page: nothing but a raster image. `overlay_words` (from the
    source PDF's text layer) adds the INVISIBLE recognised text that OCR
    software lays over a scan, in render mode 3."""
    data, _ = encode(img, jpeg_quality)
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(page.rect, stream=data)
    for x0, y0, x1, y1, text, *_ in overlay_words or []:
        page.insert_text((x0, y1 - 2), text, fontsize=max(4.0, (y1 - y0) * 0.8), render_mode=3)
    # deflate: without it PyMuPDF stores a decoded PNG raw (a bilevel 300 DPI
    # page came out at 24 MB, over Module 1's upload cap) - a generator
    # artefact, not something a real scanner produces.
    out = doc.tobytes(deflate=True)
    doc.close()
    return out


# --- degradations ------------------------------------------------------------


def skew(img: np.ndarray, degrees: float) -> np.ndarray:
    h, w = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(img, matrix, (w, h), borderValue=(255, 255, 255))


def add_noise(img: np.ndarray, sigma: float, seed: int) -> np.ndarray:
    noise = np.random.default_rng(seed).normal(0, sigma, img.shape)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def speckle(img: np.ndarray, fraction: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = img.copy()
    mask = rng.random(img.shape[:2])
    out[mask < fraction / 2] = 0
    out[mask > 1 - fraction / 2] = 255
    return out


def contrast(img: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    return np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)


def gamma(img: np.ndarray, g: float) -> np.ndarray:
    return (255.0 * (img.astype(np.float32) / 255.0) ** g).astype(np.uint8)


def bilevel(img: np.ndarray, threshold: int = 170) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


# --- the camera-photo simulator ---------------------------------------------


def photograph(
    page: np.ndarray,
    corner_offsets,
    *,
    margin: float = 0.28,
    brightness: float = 1.0,
    blur: int = 0,
    shadow: float = 0.0,
    noise: float = 0.0,
    jpeg_quality: int = 88,
    downscale: float = 1.0,
    seed: int = 0,
) -> bytes:
    """The page placed on a desk surface and 'photographed': warped by
    `corner_offsets` (pixels moved at each of the four corners), then lit,
    blurred, noised, shrunk and JPEG-compressed like a phone would."""
    h, w = page.shape[:2]
    cw, ch = int(w * (1 + 2 * margin)), int(h * (1 + 2 * margin))
    rng = np.random.default_rng(seed)

    desk = np.full((ch, cw, 3), (78, 105, 130), np.uint8)
    canvas = np.clip(desk + rng.normal(0, 9, (ch, cw, 3)), 0, 255).astype(np.uint8)

    src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], "float32")
    ox, oy = int(w * margin), int(h * margin)
    dst = np.array([[ox, oy], [ox + w - 1, oy], [ox + w - 1, oy + h - 1], [ox, oy + h - 1]], "float32")
    dst += np.array(corner_offsets, "float32")
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, matrix, (cw, ch), borderMode=cv2.BORDER_TRANSPARENT)
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), matrix, (cw, ch))
    canvas[mask > 0] = warped[mask > 0]

    if shadow:  # light falling off across the frame, as with one overhead lamp
        falloff = np.linspace(1.0, 1.0 - shadow, cw, dtype=np.float32)[None, :, None]
        canvas = np.clip(canvas.astype(np.float32) * falloff, 0, 255).astype(np.uint8)
    if brightness != 1.0:
        canvas = np.clip(canvas.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    if blur:
        canvas = cv2.GaussianBlur(canvas, (blur, blur), 0)
    if noise:
        canvas = add_noise(canvas, noise, seed + 1)
    if downscale != 1.0:
        canvas = cv2.resize(canvas, None, fx=downscale, fy=downscale, interpolation=cv2.INTER_AREA)
    return encode(canvas, jpeg_quality)[0]


def build(source_pdf: Path, ground_truth_file: Path, out_dir: Path) -> dict:
    truth = json.loads(ground_truth_file.read_text(encoding="utf-8"))
    entry = next(
        (v for k, v in truth.items() if k.lower().endswith(".pdf") and v.get("template_id") == "air_waybill"), None
    )
    if entry is None:
        raise SystemExit(f"no air_waybill PDF entry found in {ground_truth_file}")
    fields = entry["fields"]

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(source_pdf)
    source_words = doc[0].get_text("words")
    page200 = render_page(doc, 200)

    files: dict[str, dict] = {}

    def write(name: str, data: bytes, **extra) -> None:
        (out_dir / name).write_bytes(data)
        files[name] = {"template_id": "air_waybill", "fields": fields, **extra}

    # The control: the real PDF, untouched.
    write("reference__original_digital.pdf", source_pdf.read_bytes())

    # --- tier 2: scanned / flat-image PDFs ---
    write("synthetic__scan_clean_200dpi.pdf", image_only_pdf(render_page(doc, 200)))
    write("synthetic__scan_150dpi.pdf", image_only_pdf(render_page(doc, 150)))
    write("synthetic__scan_100dpi.pdf", image_only_pdf(render_page(doc, 100)))
    write(
        "synthetic__scan_skew1p5_noise.pdf",
        image_only_pdf(add_noise(skew(page200, 1.5), 12, 11)),
    )
    write(
        "synthetic__scan_skew_neg2p5_jpegq55.pdf",
        image_only_pdf(skew(page200, -2.5), jpeg_quality=55),
    )
    write("synthetic__scan_faded.pdf", image_only_pdf(contrast(page200, 0.55, 95)))
    write("synthetic__scan_dark_speckle.pdf", image_only_pdf(speckle(gamma(page200, 1.9), 0.004, 12)))
    write("synthetic__scan_bilevel_300dpi.pdf", image_only_pdf(bilevel(render_page(doc, 300)), jpeg_quality=None))
    # Detected as a text-layer PDF by "get_text() is non-empty", but it is a scan:
    # the ground truth says so, and the pipeline must read it through OCR.
    write(
        "synthetic__scan_with_ocr_overlay.pdf",
        image_only_pdf(page200, overlay_words=source_words),
        tier="scanned_pdf",
    )

    # --- tier 3: camera JPEGs ---
    s = page200.shape[1] * 0.06  # perspective magnitude, in pixels
    zero = [[0, 0]] * 4
    rotated = [[s * .9, -s * .5], [s * .5, s * .9], [-s * .9, s * .5], [-s * .5, -s * .9]]
    keystone = [[s, s * .5], [-s, s * .5], [-s * .3, 0], [s * .3, 0]]
    corner = [[s * 1.4, s * .8], [-s * .4, -s * .3], [-s * 1.1, s * .2], [s * .2, -s * .6]]
    mild = [[s * .5, -s * .2], [s * .2, s * .4], [-s * .4, s * .2], [0, 0]]

    write("synthetic__photo_flat.jpg", photograph(page200, zero, seed=1))
    write("synthetic__photo_rotated.jpg", photograph(page200, rotated, seed=2))
    write("synthetic__photo_keystone.jpg", photograph(page200, keystone, seed=3))
    write("synthetic__photo_corner_perspective.jpg", photograph(page200, corner, seed=4))
    write("synthetic__photo_dim_blur.jpg", photograph(page200, mild, brightness=.55, blur=5, seed=5))
    write("synthetic__photo_lighting_gradient.jpg", photograph(page200, mild, shadow=.5, seed=6))
    write(
        "synthetic__photo_noisy_lowq.jpg",
        photograph(page200, mild, noise=11, jpeg_quality=55, downscale=.6, seed=7),
    )
    write(
        "synthetic__photo_harsh_combined.jpg",
        photograph(page200, rotated, shadow=.4, brightness=.7, blur=3, noise=8, jpeg_quality=60, seed=8),
    )
    write("synthetic__photo_tight_framing.jpg", photograph(page200, mild, margin=0.04, seed=9))
    write("synthetic__photo_wide_framing.jpg", photograph(page200, mild, margin=0.6, seed=10))

    doc.close()
    (out_dir / "ground_truth.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures-dir", type=Path, default=PROJECT_ROOT / "tests" / "fixtures")
    parser.add_argument("--source", type=Path, help="the real waybill PDF (default: the air_waybill PDF named in the ground truth)")
    parser.add_argument("--out-dir", type=Path, default=None, help="default: <fixtures-dir>/synthetic_awb")
    args = parser.parse_args()

    ground_truth_file = args.fixtures_dir / "ground_truth.json"
    truth = json.loads(ground_truth_file.read_text(encoding="utf-8"))
    source = args.source or next(
        args.fixtures_dir / k for k, v in truth.items() if k.lower().endswith(".pdf") and v.get("template_id") == "air_waybill"
    )
    out_dir = args.out_dir or args.fixtures_dir / "synthetic_awb"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    files = build(source, ground_truth_file, out_dir)

    print(f"wrote {len(files)} files to {out_dir}")
    for name in files:
        print(f"  {name}  ({(out_dir / name).stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
