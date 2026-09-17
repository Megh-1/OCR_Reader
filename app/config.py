import os

MAX_UPLOAD_BYTES: int = 15 * 1024 * 1024
MAX_PAGES: int = 20
ALLOWED_MIME_TYPES: set[str] = {"application/pdf", "image/jpeg", "image/png"}

# Module 3/4 — Template Store
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://idp:idp@localhost:5432/idp_pipeline",
)

# Module 2 — Preprocessing tunables

# Resolution normalization. Every downstream template's field coordinates are
# relative percentages of page dimensions, so this pair is what keeps them
# meaningful across scans of differing quality.
TARGET_DPI: float = 300.0
DEFAULT_SOURCE_DPI: float = 72.0  # assumed when a source image carries no DPI metadata

# Caps TARGET_DPI/source_dpi regardless of what source_dpi claims to be.
# Real-world discovery: phone cameras near-universally embed a meaningless
# legacy "72 DPI" JFIF tag with no relation to the actual pixel density of
# whatever was photographed - trusting it verbatim on an already-high-
# resolution photo computes a ~4x upscale, which on a modern phone photo
# is easily enough to trip Pillow's decompression-bomb safety limit before
# the image ever reaches Module 5. This still lets genuinely low-quality
# scans get upscaled up to this factor, which is this step's actual job
# per the reference doc - it just refuses to trust an implausible claim.
MAX_UPSCALE_FACTOR: float = 2.0

# Skew estimation (contour-based, with an OSD fallback).
MIN_CONTOUR_AREA_FRACTION: float = 0.01  # below this, the contour mass isn't trusted
SKEW_DILATE_KERNEL_SIZE: int = 5
SKEW_DILATE_ITERATIONS: int = 3
SKEW_ANGLE_NOOP_THRESHOLD: float = 0.1  # skip rotation entirely below this, to avoid needless blur

# Denoise / contrast (cv2.fastNlMeansDenoising + cv2.adaptiveThreshold).
DENOISE_STRENGTH: float = 10.0
ADAPTIVE_THRESH_BLOCK_SIZE: int = 35  # must be odd and > 1
ADAPTIVE_THRESH_C: int = 11

# Module 5 — Document Type Identification

# Anchor cropping had no margin at all until a real photo exposed why that's
# a problem: a zero-slack crop is fragile against Tesseract's page
# segmentation getting thrown off by a few stray edge pixels (residual
# tilt, sub-pixel rounding) - confirmed by direct experiment on a real
# card, where the same anchor's OCR score oscillated wildly (good/0/good)
# at small margins and only stabilized once the margin was generous. Wider
# than FIELD_CROP_MARGIN_PX because anchor lines are typically much wider
# than a single field value, so the same absolute pixel margin matters less
# relative to their size - and because this was calibrated at whatever
# resolution Module 2 actually normalizes to, which varies (see
# MAX_UPSCALE_FACTOR above); a real-world value from one sample, not a
# theoretically-derived constant.
ANCHOR_CROP_MARGIN_PX: int = 40

# Module 6 — Field Extraction

FIELD_CROP_MARGIN_PX: int = 8  # small fixed margin absorbing sub-pixel jitter, per the reference doc

# The template schema has no per-field PSM slot, so this stands in for it:
# a fixed data_type -> Tesseract --psm mapping. 7 (single line) and 8
# (single word) are the reference doc's own examples. Anything not listed
# here falls back to DEFAULT_PSM, never silently to one of these.
PSM_BY_DATA_TYPE: dict[str, int] = {
    "string": 7,
    "enum": 8,
}
DEFAULT_PSM: int = 6  # Tesseract's general "assume a uniform block of text" mode

# Optional absolute path to the tesseract binary, for environments where
# it's installed but not reliably on PATH (seen on this machine: a
# winget-installed Tesseract that never added itself to PATH). Leave unset
# to trust PATH resolution, which is what the Docker image relies on -
# this is a local-environment escape hatch, not something to depend on.
#
# The filesystem-existence fallback (not just the env var) exists because
# environment variable changes on this machine have repeatedly failed to
# reach freshly-spawned processes this session (PATH edits invisible to
# already-running shells, etc.) - a direct file check at import time is
# immune to that class of problem, unlike an env var someone forgot to
# (or couldn't) set for a given process. Inert on Linux/Docker, where the
# path simply won't exist and this falls through to None.
_DEFAULT_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD: str | None = os.environ.get("TESSERACT_CMD") or (
    _DEFAULT_WINDOWS_TESSERACT if os.path.exists(_DEFAULT_WINDOWS_TESSERACT) else None
)
