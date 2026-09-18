import os

MAX_UPLOAD_BYTES: int = 15 * 1024 * 1024
MAX_PAGES: int = 20
ALLOWED_MIME_TYPES: set[str] = {"application/pdf", "image/jpeg", "image/png"}

# PyMuPDF renders at 72 DPI unless told otherwise, which is far too coarse to
# OCR: measured on a real air waybill, a 72-DPI render recovered 2 of 9
# known field values at mean confidence 57, against 9 of 9 at 88 for 200.
# Higher is not monotonically better - 300 dropped back to 7 of 9, since
# oversized glyphs start working against Tesseract's line segmentation - so
# this is a measured choice, not "as high as we can afford".
PDF_RENDER_DPI: int = 200

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

# Upscaling past a point actively destroys OCR accuracy: interpolation adds
# no information, only softened glyph edges that work against Tesseract's
# classifier. Measured on a real air waybill, same page, scale alone varying:
#
#   long edge 1108 -> 9/9 known values      long edge 2771 -> 8/9
#   long edge 1663 -> 8/9                   long edge 3326 -> 6/9
#   long edge 2217 -> 8/9                   long edge 4434 -> 6/9
#
# corroborated independently by the PDF render-DPI sweep, where 300 DPI
# scored worse than 200. This caps magnification at roughly a letter page at
# 200 DPI. It never shrinks an image: on the ID card, native resolution
# measured better than any downscale (confidence 91/96 against 86/84), and
# throwing away real captured detail is a different mistake from declining
# to invent fake detail.
OCR_TARGET_LONG_EDGE_PX: int = 2400

# Document detection / perspective normalization.
#
# A photographed page is rotated, perspective-distorted, and framed
# differently every time. Since every template bbox is a percentage of the
# PAGE, none of those coordinates mean anything until the page itself is
# located and flattened - this step is what makes a coordinate template
# usable on a handheld photo rather than only on a scan.
#
# Gated deliberately: it fires only when a convincing four-cornered document
# is found, and passes the image through untouched otherwise. The upper area
# bound matters as much as the lower one - a scan or a PDF render where the
# page already fills the frame has no border to find, and "detecting" one
# there would mean warping to something that isn't the document edge.
DOC_DETECT_MIN_AREA_FRACTION: float = 0.15
DOC_DETECT_MAX_AREA_FRACTION: float = 0.98
DOC_DETECT_WORKING_WIDTH: int = 1000  # detection runs downscaled; corners scale back up
DOC_DETECT_APPROX_EPSILON: float = 0.02  # arc-length fraction for polygon simplification
DOC_DETECT_MIN_ASPECT: float = 0.25  # reject slivers; a page is never this elongated
DOC_DETECT_MAX_ASPECT: float = 4.0

# Mean-intensity gap required across a candidate page edge (0-255). The
# strongest rectangle in a scan or a PDF render is the form's own printed
# border - a valid convex quad of plausible size - and warping to it crops
# away everything outside the frame. A photographed page sits on something
# (desk, table) so a real document edge has contrast across it; a printed
# rule on white paper does not. Measured on this project's own inputs, that
# gap is a few points for a PDF render against well over a hundred for a
# photo, so the threshold sits in a very wide gap rather than on a knife edge.
DOC_DETECT_MIN_BORDER_CONTRAST: float = 25.0

# Skew estimation (contour-based, with an OSD fallback).
MIN_CONTOUR_AREA_FRACTION: float = 0.01  # below this, the contour mass isn't trusted
SKEW_DILATE_KERNEL_SIZE: int = 5
SKEW_DILATE_ITERATIONS: int = 3
SKEW_ANGLE_NOOP_THRESHOLD: float = 0.1  # skip rotation entirely below this, to avoid needless blur

# Ruled-line removal for the field-extraction rendering. A stroke counts as a
# rule only if it runs at least 1/N of the page width or height, which is far
# longer than any glyph - that ratio is what separates "table border" from
# "letter", and it holds at any resolution because it is relative.
RULE_MIN_LENGTH_DIVISOR: int = 30
RULE_DETECT_BLOCK_SIZE: int = 15
RULE_DILATE_PX: int = 1

# Local contrast equalization for the field-extraction rendering (CLAHE).
# OpenCV's conventional defaults - deliberately not fitted to a sample.
CLAHE_CLIP_LIMIT: float = 2.0
CLAHE_TILE_GRID: int = 8

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

# Module 8 — Output Compiler

# A stored page render only has to be legible to a human in the review
# canvas (a few hundred px wide), not to OCR - full-resolution pages run to
# tens of megabytes each. Every bbox is a relative percentage, so a
# downscaled render addresses exactly the same regions.
REVIEW_IMAGE_MAX_WIDTH: int = 1400
REVIEW_IMAGE_JPEG_QUALITY: int = 82

# Module 6 — Field Extraction

# Characters that are never the first or last character of a real field
# value, but routinely appear there as fragments of a form's printed cell
# border. Edge-trimmed only - see _trim_rule_artifacts.
RULE_ARTIFACT_CHARS: str = r"|_~[]{}()/\:;`^"

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
