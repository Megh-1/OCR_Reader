import os
import shutil
from pathlib import Path

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
# Where else (dx, dy in pixels of the page image) an anchor that failed at its own position is read again, best score kept, and only
# when its first score is within ANCHOR_RETRY_BAND points of its threshold (a near miss; a different form scores far below).
# MEASURED on 66 waybill pages (50 demo + 16 real, no hint) plus the ID card: three valid waybills that were refused now pass
# (demo_m_15 anchor 68.2 -> 88.5, demo_m_24 48.4 -> 70.8, real #7 67.6 -> 83.3), none changed the other way, no page became
# ambiguous, the ID card still reads as itself; the weakest anchor of the OTHER form's template rose from 42.4 to 54.5 at most (70
# needed). Without the retries, moving the boxes ~1 px by anything (the cross terms) refused two confirmed pages. PROVISIONAL.
# ((0, 0),) turns it off.
ANCHOR_OFFSETS_PX: tuple[tuple[int, int], ...] = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (2, 0), (-2, 0), (0, 2), (0, -2))
ANCHOR_RETRY_BAND: float = 30.0

# Page alignment (app/modules/alignment.py) - ALL PROVISIONAL: measured on 16 real air
# waybills from three printers plus the reference, no negative set beyond them.
#
# A template stores boxes as page fractions, which assumes the form sits at the same place on
# every page. Real files break that (A4 vs Letter, scan margins, a form printed 4% lower): of 16
# real waybills one matched its anchors, all 15 others were refused with the anchor crop reading
# blank paper. Alignment finds the template's printed words on the page and moves every box by the
# shift and stretch they imply. Measured: 20-105 matching words per same-layout page agreeing to
# about 0.4% of the page, 10 on a different vendor's form.
#
# Words shorter than this, or read at lower confidence, are not used as landmarks: short tokens
# match by accident and low-confidence reads are mostly noise.
ALIGN_MIN_WORD_LEN: int = 4
ALIGN_MIN_WORD_CONF: float = 30.0
# Full-page OCR mode. 3 and 11 found the same labels (psm 3 slightly more on the multi-page
# files) and 3 is the Tesseract default.
ALIGN_OCR_PSM: int = 3
# A page word is the same landmark when its letters are equal, or within this fuzz ratio when the
# lengths are within 2 (OCR drops and swaps characters).
ALIGN_MATCH_RATIO: int = 88
# RANSAC inlier tolerance, in page fractions. Loose on purpose: a form version whose columns sit
# elsewhere is rejected by the minimum count below, not by a tight tolerance, and the local
# correction handles genuine small distortion.
ALIGN_RANSAC_TOL: float = 0.02
ALIGN_RANSAC_ITERS: int = 3000
# Fewest distinct landmarks that must agree, and the fraction of the template's landmarks. The
# different-vendor form agreed on 10; same-layout pages on 20 or more.
ALIGN_MIN_LANDMARKS: int = 12
ALIGN_MIN_LANDMARK_FRACTION: float = 0.15
# The fit is refused when the page would have to be scaled outside this range on either axis
# (A4 against Letter measured 0.91-0.94 vertically).
ALIGN_SCALE_RANGE: tuple[float, float] = (0.75, 1.3)
# Each box is moved by the median leftover (against the global fit) of the landmarks within
# ALIGN_LOCAL_RADIUS of it (page fractions), looking at no more than ALIGN_LOCAL_NEIGHBOURS of them, and
# only when at least ALIGN_LOCAL_MIN_NEIGHBOURS are in range; otherwise the box takes the global fit alone.
# MEASURED on the 10 labelled Bangkok-form pages (anchors refused, and value centres inside their mapped box,
# of 54 findable values):
#   global fit only                          2 refused   30/54
#   8 nearest, plain median (first version)  1 refused   28/54
#   within 0.20, distance-weighted, min 2    4 refused   33/54   <- let one mismatched pair win; reverted
#   within 0.20, plain median, min 3         1 refused   33/54   <- this one
#   within 0.20, plain median, min 4         1 refused   32/54
# The one refused page in the last rows is a different print of the form (expected). PROVISIONAL: one form.
# The global fit can also take a cross term per axis (page x moves with template y, page y with template x: what a small residual
# page tilt does) when it stays within this many page fractions per unit (0.03 ~ 1.7 degrees). 0 = OFF, the shipped setting.
# MEASURED at 0.03 on the 21 scanned/photographed demo waybills (652 fields with a known true position): values fully inside the
# tightest window 614 -> 622, misses on pages tilted 0.4 degrees or more 22 -> 14, one value lost; fit rms on demo_m_02 0.0031 -> 0.0019.
# But Module 5 outcomes on 66 pages (50 demo + 16 real waybills, no hint): 3 flipped - two confirmed pages refused (demo_m_02, a real
# Mumbai-form scan: anchor boxes moved ~1 px and a marginal anchor, 76.5 against 70, read 0.0) and one refused page accepted (real
# #7, 67.6 before). The anchors, not the alignment, are what is fragile there, so it stays off until anchor scoring tolerates a 1 px
# move; set it to try again. UPDATE: with the anchor retries below (ANCHOR_OFFSETS_PX) and NO near-miss band the same 66 pages
# lose nothing with the cross terms on, but demo_m_02's anchor read 0.0 (Tesseract's layout analysis failed on the shifted crop, not a
# near miss), so under ANCHOR_RETRY_BAND = 30 it is still refused. Also unmeasured on real photographs and on a form with one displaced
# block, where it moved a far box 0.006 (tests/test_alignment.py). Left off.
ALIGN_MAX_CROSS: float = 0.0
ALIGN_LOCAL_NEIGHBOURS: int = 8
ALIGN_LOCAL_RADIUS: float = 0.20
ALIGN_LOCAL_MIN_NEIGHBOURS: int = 3

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

# Text-layer registration (app/modules/layer_registration.py; tesseract loop, iteration 1), ON since it was measured: DEV digital 320 -> 607 of 792
# fields right, wrong at auto 17 -> 9 (registered values are capped at spot_check), SANITY real digital unchanged 110/132, scans and photos
# untouched (91/561, 22/264), 5.5 -> 5.4 docs/min. The typed layer of a pre-printed form sits off its printed cells by up to ~0.025 across and ~0.04 down, zone by zone (scripts/drift.py,
# taken from the real scans); the search window is those maxima. A field's shift is the one that puts the most typed words in boxes
# among the fields within NEIGHBOURHOOD of it (page-height fraction), used only if it beats the unshifted position by MIN_GAIN words.
TEXT_LAYER_REGISTRATION: bool = os.environ.get("TEXT_LAYER_REGISTRATION", "1") != "0"
TEXT_LAYER_REG_MAX_DX: float = 0.01   # the real scans drift mostly down/up; across is a few thousandths (scripts/drift.py)
TEXT_LAYER_REG_MAX_DY: float = 0.04
TEXT_LAYER_REG_STEP: float = 0.0025
TEXT_LAYER_REG_STEP_X: float = 0.005
TEXT_LAYER_REG_NEIGHBOURHOOD: float = 0.06
TEXT_LAYER_REG_MIN_GAIN: float = 2.0
TEXT_LAYER_REG_FORMAT_WEIGHT: float = 2.0
TEXT_LAYER_REG_MAX_BOX_HEIGHT: float = 0.03

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
# accepts is "auto" only when the template gives it a regex or a checksum AND its
# confidence is at or above this; otherwise "spot_check": it passed every check, but
# Tesseract was not very sure, or confidence was the only thing that judged it.
# 0.0-1.0, the same scale as a template field's confidence_floor.
#
# Why a validator is required: every wrong value Module 7 ever accepted on the
# synthetic corpus (shipper_name 'TE' for 'TF' and label bleed, flight_date 'EKO565'
# for 'EK0565', declared_value_customs 'NVG' for 'NVC', accounting_information with
# stray leading characters) was in a field with no regex or checksum, at confidence
# 64-87. Only a 3-5 point margin under 0.90 kept them out of auto; a misread at 91
# would have shipped. Now no confidence can auto-approve such a field. flight_date
# has since gained a regex, which rejects 'EKO565' outright.
#
# PROVISIONAL, measured on SYNTHETIC input. The 20-file synthetic waybill corpus plus
# the two real files, through POST /documents/batch on the Docker stack (Debian
# Tesseract), at 0.90 - fields auto / spot_check / review, then auto precision:
#   text-layer PDF     38 /  20 /  0 of 58   38/38    (58 / 0 / 0 before the rule)
#   scanned PDF       136 / 112 / 13 of 261  136/136  (218 / 30 / 13)
#   camera-photo JPEG 148 / 115 / 30 of 293  148/148  (232 / 32 / 29; 3 reads are real)
# The drop is 186 reads held at spot_check for having no validator, ALL correct - the
# rule's cost on this corpus; its benefit is the margin above, which the corpus never
# tested. Text-layer reads are included: their fixed 100 says the PDF held those words,
# not that the box caught the right ones. Auto precision is now 100% at every threshold
# from 0.50 to 0.96 in every tier, because no validated field produced an accepted wrong
# value - so on this corpus the threshold guards nothing observed. It still guards a
# shape-valid misread in a validated field (a weight digit read as another digit), which
# this corpus cannot measure: it is renderings of ONE waybill (29 distinct values, 32 with
# the ID card), so "100%" cannot demonstrate the 99% target, only fail to contradict it.
# Real photographed waybills are unmeasured.
CONFIDENCE_AUTO_APPROVE_THRESHOLD: float = 0.90

# Audited auto-approval (app/modules/trust.py). A field whose spot checks have been looked at by a person ONE AT A
# TIME often enough, with few enough changes, that the exact upper bound on its error rate is at or under
# TRUST_TARGET_ERROR_RATE (at TRUST_CONFIDENCE), stops sending its spot checks to the queue: they are approved as
# `auto` (auto_basis "trusted_field"), except a random TRUST_AUDIT_FRACTION that still goes to a person. Those audits
# keep the evidence current, so one wrong value found raises the bound and takes the trust away on its own.
# At 1 in 10,000 and 95% it takes 29,956 clean checks of a field, so nothing is trusted until a person has really
# checked that many; 0.05 is the share of a trusted field kept under audit, a starting figure to re-measure.
# TRUST_CACHE_SECONDS only spares the database one aggregate query per page.
AUDITED_AUTO_APPROVAL: bool = os.environ.get("AUDITED_AUTO_APPROVAL", "1") != "0"
TRUST_TARGET_ERROR_RATE: float = float(os.environ.get("TRUST_TARGET_ERROR_RATE", "0.0001"))
TRUST_CONFIDENCE: float = 0.95
TRUST_AUDIT_FRACTION: float = float(os.environ.get("TRUST_AUDIT_FRACTION", "0.05"))
TRUST_CACHE_SECONDS: float = 30.0

# Vision-model reader (app/modules/vlm_reader). A template whose hints name a `vlm_reader` profile is read by a fine-tuned vision
# model through a SEPARATE service (scripts/gpu/serve_reader.py, on a machine with a GPU) instead of by per-field Tesseract. It is
# OFF unless VLM_READER_URL is set, and any failure (service down, profile missing, page not aligned) falls back to the
# Tesseract path for that page, logged and reported in the page result. The crops of a page leave this machine for the service.
VLM_READER_URL: str = os.environ.get("VLM_READER_URL", "").rstrip("/")
VLM_READER_TOKEN: str = os.environ.get("VLM_READER_TOKEN", "")
# Measured on a shared RTX 5090 through an SSH tunnel: 24 crops answer in ~7 s (0.27-0.31 s a crop), so 120 s is ~17x headroom for one
# request. A dead tunnel does not refuse the connection, it hangs: with a long timeout every page of a batch waited the whole of it
# (130 s per page at 120 s) before the Tesseract fallback ran, so connecting gets its own short limit.
VLM_READER_TIMEOUT_S: float = float(os.environ.get("VLM_READER_TIMEOUT_S", "120"))
VLM_READER_CONNECT_TIMEOUT_S: float = float(os.environ.get("VLM_READER_CONNECT_TIMEOUT_S", "10"))
VLM_READER_CROPS_PER_REQUEST: int = int(os.environ.get("VLM_READER_CROPS_PER_REQUEST", "24"))
# Which model reads the crops is a registry choice (app/modules/vlm_reader/engines.py): READER_ENGINES_FILE is a JSON file of named
# engines (config/reader_engines.example.json), READER_ENGINE picks one by name (else the file's `default`, else its first). Unset, the
# VLM_READER_URL / VLM_READER_TOKEN above make a single engine called "default", so existing deployments are unchanged.
READER_ENGINES_FILE: str = os.environ.get("READER_ENGINES_FILE", "")
READER_ENGINE: str = os.environ.get("READER_ENGINE", "")
# Engine selection (app/modules/engine_selection, docs/engine-selection.md): which engines read a template, and in what order, is
# decided from MEASUREMENTS (scorecards from scripts/evaluate_reader.py --save), not from the order of the registry file. OFF by default:
# off, a page is read by the one engine READER_ENGINE names, exactly as before. On, a template with no promoted engine is read in
# cold-start mode (a default chain, nothing reaches `auto`). Every figure below is a PROVISIONAL starting value, not a measurement.
ENGINE_SELECTOR_ENABLED: bool = os.environ.get("ENGINE_SELECTOR_ENABLED", "").lower() in ("1", "true", "yes")
ENGINE_CHAIN_OVERRIDES_FILE: str = os.environ.get("ENGINE_CHAIN_OVERRIDES_FILE", "")  # {"template_id": ["engine", ...]}; unset = off
ENGINE_MIN_FIELDS: int = int(os.environ.get("ENGINE_MIN_FIELDS", "300"))  # a scorecard on fewer fields ranks nothing
ENGINE_ACCURACY_TIE: float = 0.005  # accuracies this close are a tie, broken by speed (half a point is inside one corpus's noise)
ENGINE_PROMOTE_MARGIN: float = 0.01  # a candidate must beat the incumbent by this much on the SAME corpus
ENGINE_DRIFT_WINDOW: int = 200  # the most recent people-checked values of a (template, engine) that drift is judged on
ENGINE_DRIFT_MIN_CHECKS: int = 50  # fewer checks than this say nothing
ENGINE_DRIFT_MARGIN: float = 0.05  # live accuracy this far under the scorecard's takes the engine out of the chain
ENGINE_AGREEMENT_MIN_AGREED: int = 200  # agreed fields a pair must have been measured on before agreement may raise trust
ENGINE_AGREEMENT_MAX_WRONG_RATE: float = 0.005  # ...and at most this share of what they agreed on may be wrong
ENGINE_SHARP_MIN: float = 60.0  # Laplacian variance of the page at 1200 px wide: under this a page is `soft`
ENGINE_SHARP_POOR: float = 20.0  # ...and under this `poor`. Both provisional: set on synthetic pages only
VLM_READER_PROFILE_DIR: str = os.environ.get("VLM_READER_PROFILE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "reader_profiles"))

# Page engines (app/modules/page_reader.py, page_tiers.py, page_pipeline.py): pretrained vision models that read a whole waybill page with NO
# template. READER_PAGE_ENGINES is a comma-separated list of engine names (role "page" in READER_ENGINES_FILE), the first is the PRIMARY reader and
# the others are second readers for agreement; empty = page mode is off and every page goes through the template path, exactly as before.
# READER_MODE: "page_first" (the page engines read every air waybill page; a matching template adds its own read as a second reader) or
# "template" (page mode off). READER_CROP_ENGINE: "template_path" (the template's own read, text layer or Tesseract, as a second reader when a
# template matched) or "none". READER_PAGE_NORMALISE / _SHARPER / _HALVES / _LAYER are the variants measured on DEV (docs/reader-results.md).
READER_MODE: str = os.environ.get("READER_MODE", "page_first")
READER_PAGE_ENGINES: list[str] = [n.strip() for n in os.environ.get("READER_PAGE_ENGINES", "page_qwen3vl,page_glmocr").split(",") if n.strip()]
READER_CROP_ENGINE: str = os.environ.get("READER_CROP_ENGINE", "template_path")
READER_PAGE_NORMALISE: bool = os.environ.get("READER_PAGE_NORMALISE", "1") != "0"
READER_PAGE_SHARPER: bool = os.environ.get("READER_PAGE_SHARPER", "1") != "0"
READER_PAGE_HALVES: bool = os.environ.get("READER_PAGE_HALVES", "1") != "0"
READER_PAGE_LAYER: bool = os.environ.get("READER_PAGE_LAYER", "1") != "0"
# The model reports no confidence, so each value gets a FIXED one (as TEXT_LAYER_CONFIDENCE does), chosen to put it in a tier:
#   GATED    a second reader agrees, or an arithmetic check of the document holds, and the format passes. Under
#            CONFIDENCE_AUTO_APPROVE_THRESHOLD*100 (90) on purpose: such a value is accepted but still shown to a person as a spot
#            check. Measured gate precision: 0 wrong of 236 auto-eligible fields on real pages, but 2 wrong of 955 arithmetic-gated
#            fields on synthetic pages (0.2%), which is NOT the 1 in 10,000 the project aims at. Raising this to 90 or more makes
#            gated values auto; do that only with eyes open, or let audited auto-approval (trust.py) earn it field by field.
#   UNGATED  anything else the model read (measured ~89% right on new real pages): accepted above the field's floor, a spot check.
# A blank is 0 (rejected) unless the field is optional, where a blank is a legitimate answer and takes the ungated value.
VLM_GATED_CONFIDENCE: float = float(os.environ.get("VLM_GATED_CONFIDENCE", "89"))
VLM_UNGATED_CONFIDENCE: float = float(os.environ.get("VLM_UNGATED_CONFIDENCE", "70"))

# Threads each Tesseract PROCESS may use (its OpenMP OMP_THREAD_LIMIT). This matters far
# more than it looks, and only on Linux. Every field read is its own Tesseract process
# and a document runs FIELD_OCR_WORKERS of them at once; left alone, each also starts
# one OpenMP thread per core, and those threads spin-wait, so they starve each other.
# Measured on this machine (18 vCPUs), the 33-field OCR stage of one photographed
# waybill, in the Linux container:
#   default OpenMP, one document alone               8.7 s
#   OMP_THREAD_LIMIT=1, one document alone           1.9 s   (4.6x faster)
#   default OpenMP, 3 documents at once           186-191 s each  (a worker pool grinds to a halt)
#   OMP_THREAD_LIMIT=1, 3 documents at once        4.2-4.8 s each
#   OMP_THREAD_LIMIT=1, 6 documents at once      11.7-13.2 s each
# On Windows it makes no difference (2.4 s default, 2.5 s limited) - which is why an
# earlier local measurement concluded OpenMP threading did not matter. One thread per
# process costs nothing measurable and is what Tesseract's own guidance recommends when
# running instances in parallel. Set OMP_THREAD_LIMIT in the environment to override.
TESSERACT_OMP_THREAD_LIMIT: int = 1

# Concurrent field reads. Each OCR call is its own Tesseract subprocess over
# its own crop, so this is a pure latency win with identical results. Measured
# on a 33-field air waybill (99 calls at ~195ms): 19.3s serial, 5.1s at 4
# workers, 3.2s at 8, 2.7s at 16. Past 8 the returns flatten, so this stops
# there rather than spawning processes for another half second.
#
# Tesseract's own OpenMP threading made no difference when measured on Windows (183 vs
# 195 ms/call) - but on Linux it is decisive; see TESSERACT_OMP_THREAD_LIMIT.
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
