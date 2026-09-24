import os
import shutil

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
#
# PROVISIONAL, and measured on SYNTHETIC input only: every number below comes
# from tests/fixtures/synthetic_awb (10 photo composites, all rendered from ONE
# air waybill) and from composites built off the same page with a controlled
# desk/mat/crop. The only real camera photo we hold is the ID card, and it has
# no visible page border, so it says nothing about these except that "no quad"
# is a normal answer. Re-measure on real photographed waybills before trusting
# any of them; do not tune off one sample.
#
# DOC_DETECT_MIN_AREA_FRACTION (a.k.a. "PAGE_CONTOUR_MIN_AREA_FRACTION"): the
#   corpus photos put the page at 21% (wide framing) to 85% (tight framing) of
#   the frame. On a sweep of the same page shrunk in the frame, a quad is found
#   at 17.0% and lost at 14.5%, so 0.15 sits just under the floor of what was
#   found - this is a KNIFE EDGE for a page photographed from far away, and the
#   failure is safe (no quad -> no warp -> Module 5 decides).
# DOC_DETECT_APPROX_EPSILON (a.k.a. "PAGE_CONTOUR_APPROX_EPSILON"): swept
#   0.002-0.08 on four perspective presets; worst-corner error was identical
#   (14-16 px on a 1700 px-wide page) across the whole range, so the corpus
#   does not constrain it. 0.02 is the conventional value, not a tuned one.
#   Note that error is a systematic ~4 working-px OUTWARD bias, not noise: the
#   Canny edges are dilated by a 5x5 kernel twice before the contour is taken,
#   which pushes the outline out by exactly that much. Removing it measurably
#   helps the geometry but had a mixed effect downstream (see the page
#   detection paragraph in CLAUDE.md), so it is left as-is pending real-photo
#   data.
# DOC_DETECT_MAX_AREA_FRACTION: highest observed page fraction is 85%; 0.98 is
#   there to reject "the whole frame", not fitted to anything.
DOC_DETECT_MIN_AREA_FRACTION: float = 0.15
DOC_DETECT_MAX_AREA_FRACTION: float = 0.98
DOC_DETECT_WORKING_WIDTH: int = 1000  # detection runs downscaled; corners scale back up
DOC_DETECT_APPROX_EPSILON: float = 0.02  # arc-length fraction for polygon simplification
DOC_DETECT_MIN_ASPECT: float = 0.25  # reject slivers; a page is never this elongated
DOC_DETECT_MAX_ASPECT: float = 4.0

# Thin-band contrast required across a candidate page edge, in gray levels
# (0-255): the absolute gap between the MEDIAN brightness of a narrow band just
# inside the outline and a matching band just outside it (border_contrast in
# preprocessing.py). The strongest rectangle in a scan or a PDF render is the
# form's own printed border - a valid convex quad of plausible size - and
# warping to it crops away everything outside the frame. A photographed page
# sits on something (desk, table) so a real document edge has contrast across
# it; a printed rule has paper on both sides.
#
# It replaced a whole-inside-mean vs whole-outside-mean gap of 25. That number
# measured INK DENSITY when the candidate was a printed border (the waybill's
# own border read 9-22 against it, 3 below the gate at worst; a darkened render
# of the waybill (ink deficit 30 against its 17.6) read 29.5, and a synthetic
# page at 3.5x its ink (deficit 63) read 52.6 - both falsely accepted as a
# photographed page, and on a near-white desk the second got a WRONG quad, its
# own printed border).
#
# PROVISIONAL / synthetic-only, same as the block above: a better DESIGN, not a
# validated number, and it wants a pass on real photographed pages before
# anyone trusts 40. What was measured (10 corpus photos, 10 scans / renders of
# the same waybill, and composites of that page on a desk):
#   printed border, scans and digital render   0-3     (was 9-22 on the old measure)
#   inky pages (ink deficit 30 and 63) filling the frame   0 and 0
#   corpus photos                              76-146  (dim_blur 81, harsh 76-80 lowest)
#   page on a desk, desk brightness 232/225/215/205  23 / 30 / 40 / 50 (the same at
#                                        either ink density - it is the edge that is read)
#   dark mat under the page (the wrong quad)   71  <- the gate CANNOT reject this;
#                                                     the mat's edge is real. Module 5's
#                                                     anchors are what refuse it.
# 40 sits 13x above the worst printed border and 2x under the faintest photo. It
# was chosen to keep the OLD gate's light-desk operating point (the old 25 flipped
# at a desk of ~212 against white paper; 40 flips at ~215) while removing the
# ink dependence - so this changes what is measured, not how faint an edge is
# trusted. Placement of the bands is not sensitive: five (skip, width) pairs
# moved photo readings by at most 4 levels and scans by at most 2.
DOC_DETECT_MIN_BORDER_CONTRAST: float = 40.0

# Where the two bands sit, in working pixels (DOC_DETECT_WORKING_WIDTH wide).
# The skip clears the ~4 px by which the dilated-edge outline overshoots the true
# edge plus the edge's own blur; the width just needs to hold enough pixels for a
# stable median. Provisional, synthetic-only.
DOC_DETECT_BAND_SKIP_PX: int = 6
DOC_DETECT_BAND_WIDTH_PX: int = 8

# Skew estimation (contour-based, with an OSD fallback).
MIN_CONTOUR_AREA_FRACTION: float = 0.01  # below this, the contour mass isn't trusted
SKEW_DILATE_KERNEL_SIZE: int = 5
SKEW_DILATE_ITERATIONS: int = 3
SKEW_ANGLE_NOOP_THRESHOLD: float = 0.1  # skip rotation entirely below this, to avoid needless blur

# Deskew - the non-text-native path only (a text-native PDF is read from its
# embedded text and never reaches this).
#
# Skew is estimated by the contour method above, then by Hough line fitting
# when the contour mass is too small to trust, then by Tesseract OSD as a last
# resort. OSD is last because it cannot measure skew at all: it reports
# orientation in 90-degree steps. Measured on the synthetic corpus it said
# "rotate=0" for scans skewed 1.5 and 2.5 degrees (at orientation confidence
# 4.56 and 0.44), and errored on both camera photos, while the contour and
# Hough estimators recovered the injected angle to within 0.03 degrees.
#
# PROVISIONAL - measured on SYNTHETIC data only, and needs re-validation once
# real camera photos are available. The skews the synthetic corpus exercises
# reach 2.5 degrees for scans; everything above that, this value included, is
# an untested judgment call. Beyond it a page is more likely genuinely rotated
# than scan-skewed - a sideways or upside-down sheet - and is left as-is and
# logged rather than auto-straightened, so Module 5 refuses it honestly
# instead of a guessed rotation feeding it a plausible-looking wrong page.
DESKEW_MAX_ANGLE_CORRECTION: float = 10.0

# Hough fallback estimator. Every value below is provisional for the same
# reason as the cap, and was checked only against the synthetic air waybill.
HOUGH_WORKING_LONG_EDGE: int = 2000  # estimate on a downscaled copy; the angle is scale-free
HOUGH_ANGLE_RESOLUTION_DEG: float = 0.25
HOUGH_VOTE_THRESHOLD: int = 80
HOUGH_MIN_LINE_FRACTION: float = 0.15  # a segment must span this share of the width to count
HOUGH_MAX_LINE_GAP_FRACTION: float = 0.01
HOUGH_ANGLE_WINDOW_DEG: float = 15.0  # lines further than this from an axis are not skew evidence
HOUGH_MIN_LINES: int = 5  # fewer usable lines than this is not enough to trust an angle

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
#
# Parentheses are deliberately NOT here. They were, and that truncated a real
# value: "BANGALORE (BLR)" came back as "BANGALORE (BLR", missing its closing
# paren, and with no regex on that field it shipped unflagged. A paren is only
# an artifact when it has no partner, so _trim_rule_artifacts judges them by
# balance instead of by character.
# U+2014 (em dash) is here because Tesseract emits it for the same vertical stroke:
# seen at the edge of raw reads in 3 of 19 synthetic-corpus files, never in a real value.
RULE_ARTIFACT_CHARS: str = r"|_~[]{}/\:;`^" + "\u2014"

# Zero, measured. The reference doc suggests a small margin to absorb
# sub-pixel jitter, and that was right when template boxes were drawn loosely
# by hand. Once a template's boxes are tight to the value - derived from a
# document's own geometry rather than eyeballed - any expansion reaches into
# the neighbouring cell and drags its printed label into the crop. On the air
# waybill this one value moved exact field recovery from ~45% to ~70%
# (per variant, /29: 12,14,14,16,14,8 at 8px against 25,17,20,19,23,17 at 0),
# and the ID card, whose boxes were drawn by hand, is unharmed: its three
# fields read identically at 0, 4 and 8.
#
# Anchors are the opposite case and keep their generous margin - see
# ANCHOR_CROP_MARGIN_PX, where a zero-slack crop was measurably unstable.
# Module 6 - text-native fast path (PDFs with a real embedded text layer).
#
# A word is taken into a field when at least this fraction of ITS OWN area
# lies inside the field's box. Any-intersection would be wrong: line spacing is
# tighter than a word's box is tall, so a field box drawn tight to one line
# still grazes the line beneath it, and that whole neighbouring line would be
# pulled into the value.
TEXT_LAYER_MIN_WORD_OVERLAP: float = 0.5

# Embedded text is the document's own characters, so there is no recognition
# uncertainty to report and Module 7's confidence gate is satisfied by
# construction. That is only sound for text that really is digital-born - a
# scan carrying an OCR overlay is recognition output wearing a text layer's
# clothes, which is why ingestion refuses to treat invisible text as native.
TEXT_LAYER_CONFIDENCE: float = 100.0

FIELD_CROP_MARGIN_PX: int = 0

# The template schema has no per-field PSM slot, so this stands in for it:
# a fixed data_type -> Tesseract --psm mapping. 7 (single line) and 8
# (single word) are the reference doc's own examples. Anything not listed
# here falls back to DEFAULT_PSM, never silently to one of these.
PSM_BY_DATA_TYPE: dict[str, int] = {
    "string": 7,
    "enum": 8,
}
DEFAULT_PSM: int = 6  # Tesseract's general "assume a uniform block of text" mode

# Segmentation modes tried per field, best-confidence wins (see extract_field).
# 4 (variable-size columns), 6 (uniform block) and 7 (single line) are the
# three that actually won on a real document. The sparse-text modes 11 and 12
# are deliberately absent: they return confident FRAGMENTS - on the shipper
# field, "R SYSTEMS PVT LT" at confidence 90 against the full correct string
# at 89 - so including them would let a truncation outrank a complete read.
PSM_CANDIDATES: tuple[int, ...] = (4, 6, 7)

# One extra, digit-whitelisted candidate for a field whose validation_regex can
# match nothing BUT digits. Without it Tesseract reads a lone "1" as "{" in some
# crops, "{" is a rule-fragment character, the edge trim removes it, and the
# field comes back blank. Constrained to digits the engine has to decide between
# a "1" and the other digits instead of reaching for punctuation.
#
# PROVISIONAL, measured on the 19 OCR-path files of the SYNTHETIC corpus (one
# waybill, so one field - pieces_rcp - qualifies), replaying cached crops:
#   plain modes (4/6/7)        1 read correctly on 16 of 19
#   whitelist + PSM 7 (or 6/10) 18 of 19: rescues tight framing (conf 31) and
#                               wide framing (conf 20), never disagreed with a
#                               plain read that was already right
#   whitelist + PSM 8 or 13    7 of 19 - it reads the "1" as "4" on 8 files, at
#                               confidence 68-94 on 7 of them: a wrong value that
#                               looks right, so those modes are deliberately not used
# The rescued reads are low confidence and still fall under the field's floor, so
# Module 7 still flags them; the gain is a correct value in front of the reviewer
# rather than a blank one. Never applied to a field that can contain "." or "-"
# (a whitelist would delete them), which is why applicability is read from the
# regex rather than from the data type.
DIGIT_WHITELIST: str = "0123456789"
DIGIT_WHITELIST_PSM: int = 7

# Module 7 approval tiers. A field Module 7 REJECTS is "review" (unchanged). One it
# accepts is "auto" when its confidence is at or above this, else "spot_check": it
# passed every check but Tesseract was not very sure, so a person may want to look.
# 0.0-1.0, the same scale as a template field's confidence_floor.
#
# PROVISIONAL, and measured almost entirely on SYNTHETIC input. Swept 0.50-1.00
# with `evaluate_accuracy.py --sweep` over the 20-file synthetic waybill corpus
# plus the two real files. At 0.90 (auto / spot_check / review, share of fields;
# auto precision):
#   text-layer PDF    100% / 0% /  0%   58/58    (all conf 100 by construction)
#   scanned PDF        85% / 10% / 5%   222/222
#   camera-photo JPEG  80% / 10% / 10%  234/234  (3 of its 293 reads are real)
# Auto precision is 100% in every tier for any threshold above 0.87 (the highest
# wrong read) up to 0.96; by 0.97 the OCR tiers have almost no auto fields left
# (0 scanned, 2 camera).
#
# Read that with three cautions:
#  * MARGIN. Every wrong value Module 7 accepted (7 reads of 3 fields: shipper_name
#    'TE' for 'TF' and label bleed, flight_date 'EKO565' for 'EK0565',
#    declared_value_customs 'NVG' for 'NVC') has confidence 66-87. 0.90 is 3
#    points above the highest (87.0). A real misread at 91 would auto-approve.
#  * NOT INDEPENDENT. The corpus is renderings of ONE waybill: 29 distinct values,
#    32 with the ID card. 234 clean auto reads are ~29 samples, not 234, so "100%"
#    cannot demonstrate the 99% target; it only fails to contradict it.
#  * EVERY wrong-accepted value is in a field with no regex or checksum, so only
#    confidence stood between it and auto. Where a format check exists the auto
#    subset was 147/147 (camera) and 138/138 (scanned); where none does, 87/87 and
#    84/84 - but the errors that exist live there, and two of the three fields
#    (a name, a 3-letter code where NVG and NVC are both valid) hold text no
#    format check could tell from the right answer. Only flight_date has a
#    catchable shape.
# Real photographed waybills have not been measured at all.
CONFIDENCE_AUTO_APPROVE_THRESHOLD: float = 0.90

# Concurrent field reads. Each OCR call is its own Tesseract subprocess over
# its own crop, so this is a pure latency win with identical results. Measured
# on a 33-field air waybill (99 calls at ~195ms): 19.3s serial, 5.1s at 4
# workers, 3.2s at 8, 2.7s at 16. Past 8 the returns flatten, so this stops
# there rather than spawning processes for another half second.
#
# Tesseract's own OpenMP threading was measured and makes no difference here
# (183 vs 195 ms/call), so it is deliberately left alone.
FIELD_OCR_WORKERS: int = 8

# Batch processing: POST /documents/batch enqueues each document as an RQ job on
# Redis; a worker (python -m app.worker) runs them through the same pipeline the
# synchronous endpoint uses. Nothing here has been measured against real load.
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
BATCH_QUEUE_NAME: str = "idp-batch"
BATCH_MAX_DOCUMENTS: int = 50  # per request; bounds how many uploads are held in memory at once
# A job that raises is retried this many times, waiting these many seconds before each
# attempt (the last interval repeats). 10/60/300 rides out a Postgres or Redis restart
# without hammering it; a document that fails ingestion (wrong type, too big, corrupt)
# is NOT retried, since running the same bytes again cannot change the answer.
BATCH_MAX_RETRIES: int = 3
BATCH_RETRY_INTERVALS: tuple[int, ...] = (10, 60, 300)
# One document takes seconds (roughly 5-6 s per OCR page on this machine's synthetic
# corpus); this is a ceiling for a hung Tesseract, not a target.
BATCH_JOB_TIMEOUT: int = 600

# Optional absolute path to the tesseract binary, for environments where
# it's installed but not reliably on PATH (seen on this machine: a
# winget-installed Tesseract that never added itself to PATH). Leave unset
# to fall through to PATH, which is where the Docker image's tesseract lives -
# this is a local-environment escape hatch, not something to depend on.
#
# The filesystem-existence fallback (not just the env var) exists because
# environment variable changes on this machine have repeatedly failed to
# reach freshly-spawned processes this session (PATH edits invisible to
# already-running shells, etc.) - a direct file check at import time is
# immune to that class of problem, unlike an env var someone forgot to
# (or couldn't) set for a given process. Inert on Linux/Docker, where the
# path simply won't exist and resolution falls through to PATH.
_DEFAULT_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def resolve_tesseract_cmd(
    env: dict = os.environ, exists=os.path.exists, which=shutil.which
) -> str | None:
    """
    Where the tesseract binary is, in order: the TESSERACT_CMD environment
    variable; the default Windows install location, if the file is there; whatever
    `tesseract` is first on PATH (which is where apt puts it in the Docker image,
    /usr/bin/tesseract); otherwise None, which leaves pytesseract to try the bare
    name "tesseract" and fail loudly if it is absent.

    The Windows check comes before PATH so this machine resolves exactly as it did
    before PATH was consulted at all. On Linux the Windows path cannot exist and
    the answer is the PATH lookup - an actual path, not None, so a missing binary
    shows up as an unresolved TESSERACT_CMD rather than a mystery at first OCR.
    """
    if env.get("TESSERACT_CMD"):
        return env["TESSERACT_CMD"]
    if exists(_DEFAULT_WINDOWS_TESSERACT):
        return _DEFAULT_WINDOWS_TESSERACT
    return which("tesseract")


TESSERACT_CMD: str | None = resolve_tesseract_cmd()
