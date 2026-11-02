"""
Module 6 - Field Extraction Engine.

Runs only after Module 5 has returned confirmed=True for a specific
(template_id, version) pair. Fetches that EXACT version via Module 4's
get_template_version - never "latest" - since the version Module 5 actually
verified anchors against is the only one whose field coordinates are known
to correspond to this image.
"""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytesseract
from PIL import Image
from pytesseract import Output

from app.config import (
    DEFAULT_PSM,
    DIGIT_WHITELIST,
    DIGIT_WHITELIST_PSM,
    FIELD_CROP_MARGIN_PX,
    FIELD_OCR_WORKERS,
    PSM_BY_DATA_TYPE,
    PSM_CANDIDATES,
    RULE_ARTIFACT_CHARS,
    TEXT_LAYER_CONFIDENCE,
    TEXT_LAYER_MIN_WORD_OVERLAP,
)
from app.models.text_layer import TextLayer, TextWord
from app.modules.alignment import Alignment
from app.modules.template_store import get_template_version
from app.schemas.template import TemplateField, TemplateOut
from app.utils.geometry import clamped_crop_box


@dataclass
class FieldExtractionResult:
    field_id: str
    raw_text: str
    word_confidences: list[float]
    aggregate_confidence: float
    psm_used: int  # 0 when the value came from a text layer - no OCR mode was involved
    source: str = "ocr"  # "ocr" | "text_layer"


def _words_and_confidences(data: dict) -> tuple[list[str], list[float]]:
    """
    Pulls actual words and their confidences out of pytesseract's
    image_to_data DICT output. Tesseract marks structural rows it has no
    confidence for (page/block/paragraph/line, not real words) with an
    empty text and conf=-1; those must never be averaged in as if they were
    real low-confidence reads.
    """
    words = [text for text in data["text"] if text.strip()]
    confidences = [float(c) for c in data["conf"] if float(c) != -1]
    return words, confidences


def _aggregate_confidence(confidences: list[float]) -> float:
    return sum(confidences) / len(confidences) if confidences else 0.0


# Opening quotation marks and the closer that pairs with each. A straight quote
# is its own closer, so it is judged by parity instead.
_QUOTE_OPENERS = {"\u201c": "\u201d", "\u2018": "\u2019", '"': '"'}


def _leading_quote_is_unmatched(text: str) -> bool:
    head = text[0]
    if head not in _QUOTE_OPENERS:
        return False
    if head == '"':
        return text.count('"') % 2 == 1
    return text.count(head) > text.count(_QUOTE_OPENERS[head])


def _trim_rule_artifacts(text: str) -> str:
    """
    Strips leftover form-rule marks from the ENDS of a read value.

    A field on a ruled form sits inside a printed cell, so a crop of it tends
    to catch a fragment of the surrounding rule, which Tesseract renders as a
    stray "|", "_", "~" or bracket - "170" comes back as "| 170", "500.00" as
    "~ 500.00". Module 2 erases full-length rules before OCR; this removes the
    short fragments that survive at a crop boundary, where a rule is too short
    to be recognizable as one.

    Deliberately edge-only, and deliberately limited to characters that never
    legitimately begin or end a field value. It cannot repair a misread INSIDE
    a value, which is the whole point: undoing an artifact our own cropping
    introduced is not the same as guessing at content, and a genuine OCR error
    still reaches Module 7 intact and still gets flagged.

    Parentheses and quotation marks are the exceptions to "never legitimately
    at an edge": "BANGALORE (BLR)" ends in one, and "ACME" TRADERS begins with
    one. So they are judged by balance, not by character. A closing paren is an
    artifact only when the value has more of them than openers, an opening one
    only when it has more openers than closers, and a closer at the very start
    or an opener at the very end can never have a partner. A leading quote is an
    artifact only when nothing closes it - measured on the synthetic corpus,
    Tesseract reads a cell's left border as an opening double or single quote
    (U+201C, U+2018, or a straight quote; raw bytes e2 80 9c / e2 80 98, valid
    UTF-8, not a decoding fault) in 11 of 19 files, and that border fragment is
    never paired. A value wrapped in a matched pair survives, which also means a
    cell whose left and right borders both read as quotes would too; that
    ambiguity is inherent, and a value validator is what catches it.
    """
    text = text.strip()
    while text:
        head, tail = text[0], text[-1]
        if (
            head in RULE_ARTIFACT_CHARS
            or head == ")"
            or (head == "(" and text.count("(") > text.count(")"))
            or _leading_quote_is_unmatched(text)
        ):
            text = text[1:].lstrip()
        elif tail in RULE_ARTIFACT_CHARS or tail == "(" or (tail == ")" and text.count(")") > text.count("(")):
            text = text[:-1].rstrip()
        else:
            break
    return text


def _accepts_only_digits(pattern: str | None) -> bool:
    """
    True when `pattern` can match nothing but the characters 0-9 (and matches
    at least one character), so that restricting Tesseract to digits cannot
    delete anything the field is allowed to contain.

    Read from the parsed regex, not guessed from its text: "^\\d{1,4}$" and
    "^[0-9]+$" qualify, "^[\\d.,]{1,10}$" (a weight) and "^\\d{3}-?\\d{8}$" (an
    AWB number) do not. Anything the walk does not positively recognise counts
    as "not digit-only", so a pattern it cannot read gets today's behaviour.
    """
    if not pattern:
        return False
    try:
        from re import _constants as sre  # internal, but stable since 3.11; any failure means "no"

        parsed = re._parser.parse(pattern)
    except Exception:
        return False

    consumes = False

    def digits_only(items) -> bool:
        nonlocal consumes
        for op, arg in items:
            if op == sre.LITERAL:
                consumes = True
                if chr(arg) not in DIGIT_WHITELIST:
                    return False
            elif op == sre.IN:
                consumes = True
                for member, value in arg:
                    if member == sre.LITERAL:
                        if chr(value) not in DIGIT_WHITELIST:
                            return False
                    elif member == sre.RANGE:
                        if not all(chr(code) in DIGIT_WHITELIST for code in range(value[0], value[1] + 1)):
                            return False
                    elif member == sre.CATEGORY:
                        if value != sre.CATEGORY_DIGIT:
                            return False
                    else:  # NEGATE, or anything unrecognised
                        return False
            elif op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
                if not digits_only(arg[2]):
                    return False
            elif op == sre.SUBPATTERN:
                if not digits_only(arg[3]):
                    return False
            elif op == sre.BRANCH:
                if not all(digits_only(branch) for branch in arg[1]):
                    return False
            elif op == sre.AT:
                continue  # ^ $ \b consume nothing
            else:  # ANY, NOT_LITERAL, lookarounds, backreferences...
                return False
        return True

    return digits_only(parsed) and consumes


def _read_at_psm(crop: Image.Image, psm: int, whitelist: str | None = None) -> tuple[str, list[float], float]:
    config = f"--psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    data = pytesseract.image_to_data(crop, output_type=Output.DICT, config=config)
    words, confidences = _words_and_confidences(data)
    return _trim_rule_artifacts(" ".join(words)), confidences, _aggregate_confidence(confidences)


def extract_field(image: Image.Image, field: TemplateField) -> FieldExtractionResult:
    """
    Reads one field, trying several page-segmentation modes and keeping
    whichever Tesseract was most confident about.

    A single fixed PSM per data type - the reference doc's suggestion, and
    what this did originally - assumes one segmentation suits every field of a
    given type. Measured on a real air waybill it does not, and the mode that
    wins varies field by field with no property of the field predicting it:
    the consignee reads exactly at PSM 6 but becomes "VTS CLIMA LLG" at 7,
    while the flight number reads exactly at 4 and becomes "EKO565/27FEB" at
    6. Since Tesseract reports its own confidence, the mode can be chosen per
    field from evidence instead of guessed in advance from the schema.

    Empty reads are discarded before comparing, because an empty result can
    carry a high confidence drawn from non-text layout elements - PSM 4
    returned nothing at confidence 95 on a field two other modes read
    perfectly.

    The template's declared PSM is tried first and wins ties, so a field whose
    declared mode is already right behaves exactly as before.
    """
    box = clamped_crop_box(field.bbox, image.width, image.height, FIELD_CROP_MARGIN_PX)
    crop = image.crop(box)

    declared_psm = PSM_BY_DATA_TYPE.get(field.data_type, DEFAULT_PSM)
    candidate_modes: list[tuple[int, str | None]] = [(declared_psm, None)]
    candidate_modes += [(psm, None) for psm in PSM_CANDIDATES if psm != declared_psm]
    if _accepts_only_digits(field.validation_regex):
        # Last, so it only wins on a strictly higher confidence: a field the plain modes already
        # read keeps the plain read, and a "1" they turned into "{" (trimmed to blank) gets a
        # second chance constrained to digits. See DIGIT_WHITELIST in config.
        candidate_modes.append((DIGIT_WHITELIST_PSM, DIGIT_WHITELIST))

    best: tuple[str, list[float], float, int] | None = None
    for psm, whitelist in candidate_modes:
        text, confidences, aggregate = _read_at_psm(crop, psm, whitelist)
        if not text:
            continue
        if best is None or aggregate > best[2]:
            best = (text, confidences, aggregate, psm)

    if best is None:  # every mode came back empty - a genuinely blank region
        return FieldExtractionResult(
            field_id=field.id, raw_text="", word_confidences=[], aggregate_confidence=0.0, psm_used=declared_psm
        )

    text, confidences, aggregate, psm = best
    return FieldExtractionResult(
        field_id=field.id,
        raw_text=text,
        word_confidences=confidences,
        aggregate_confidence=aggregate,
        psm_used=psm,
    )


def _overlap_of_word(word: TextWord, box: tuple[float, float, float, float]) -> float:
    """Fraction of the WORD's own area that lies inside `box`."""
    wx0, wy0, wx1, wy1 = word.bbox
    area = (wx1 - wx0) * (wy1 - wy0)
    if area <= 0:
        return 0.0
    bx0, by0, bx1, by1 = box
    inter_w = max(0.0, min(bx1, wx1) - max(bx0, wx0))
    inter_h = max(0.0, min(by1, wy1) - max(by0, wy0))
    return inter_w * inter_h / area


def _in_reading_order(words: list[TextWord]) -> list[TextWord]:
    """Top-to-bottom by line, left-to-right within a line.

    A line is a run of words whose vertical centres sit within half a word
    height of each other - a fixed tolerance would be wrong at both extremes,
    merging tight lines of small text and splitting one line of large text.
    """
    def centre_y(w: TextWord) -> float:
        return (w.bbox[1] + w.bbox[3]) / 2

    lines: list[list[TextWord]] = []
    for word in sorted(words, key=centre_y):
        height = word.bbox[3] - word.bbox[1]
        if lines:
            line_centre = sum(centre_y(w) for w in lines[-1]) / len(lines[-1])
            line_height = max(w.bbox[3] - w.bbox[1] for w in lines[-1])
            if abs(centre_y(word) - line_centre) <= 0.5 * max(height, line_height):
                lines[-1].append(word)
                continue
        lines.append([word])
    return [w for line in lines for w in sorted(line, key=lambda w: w.bbox[0])]


def extract_field_from_text_layer(layer: TextLayer, field: TemplateField) -> FieldExtractionResult:
    """
    Reads one field straight from a PDF's embedded text: the words whose boxes
    sit inside the field's fractional bbox, in reading order. No OCR runs.

    A field box containing no words comes back empty at confidence 0, exactly
    like a blank OCR read, so Module 7 flags it rather than accepting an empty
    value - an empty field on a digital document means a misplaced box far more
    often than a genuinely blank cell.
    """
    inside = [w for w in layer.words if _overlap_of_word(w, field.bbox) >= TEXT_LAYER_MIN_WORD_OVERLAP]
    if not inside:
        return FieldExtractionResult(
            field_id=field.id, raw_text="", word_confidences=[], aggregate_confidence=0.0, psm_used=0, source="text_layer"
        )

    ordered = _in_reading_order(inside)
    return FieldExtractionResult(
        field_id=field.id,
        raw_text=" ".join(w.text for w in ordered),
        word_confidences=[TEXT_LAYER_CONFIDENCE] * len(ordered),
        aggregate_confidence=TEXT_LAYER_CONFIDENCE,
        psm_used=0,
        source="text_layer",
    )


def _fields_on_page(template: TemplateOut, alignment: Alignment | None) -> list[TemplateField]:
    """The template's fields with each box moved to where it sits on THIS page (unchanged without an alignment)."""
    if alignment is None:
        return list(template.fields)
    return [f.model_copy(update={"bbox": alignment.map_box(f.bbox)}) for f in template.fields]


def _select(field: TemplateField, text: str) -> tuple[str, bool, tuple[int, int] | None]:
    """The part of `text` this field's options select, whether a pattern (if any) matched, and the
    character span the value came from in `text` (None when the whole text is the value)."""
    matched, span = True, None
    if field.extract_pattern is not None:
        m = re.search(field.extract_pattern, text, flags=re.IGNORECASE | re.DOTALL)
        matched = m is not None
        if m:
            span = m.span(1)
            text = m.group(1).strip()
        else:
            text = ""
    if field.squash_spaces:
        text = re.sub(r"\s+", "", text)
    return text, matched, span


def _confidence_of_span(source: FieldExtractionResult, span: tuple[int, int] | None) -> tuple[float, list[float]]:
    """
    The confidence of the WORDS a value was selected from, not of the whole cell it sits in.

    A cell of ordinary words read at 95 with an HS code's digits read at 30 averages 85.7: giving the code
    the cell's average would let a misread number sit on the cell's good reading. Words are matched to
    confidences by whitespace position; when the two lists do not line up (trimming dropped a word) the
    LOWEST confidence in the cell is used instead - the conservative reading, never the generous one.
    """
    confs = source.word_confidences
    if span is None or not confs:
        return source.aggregate_confidence, list(confs)
    tokens = list(re.finditer(r"\S+", source.raw_text))
    if len(tokens) != len(confs):
        return min(confs), list(confs)
    chosen = [c for t, c in zip(tokens, confs) if t.start() < span[1] and t.end() > span[0]]
    if not chosen:
        return min(confs), list(confs)
    return sum(chosen) / len(chosen), chosen


def _apply_field_options(fields: list[TemplateField], read: dict[str, FieldExtractionResult]) -> dict[str, FieldExtractionResult]:
    """
    Turns what was READ into the field values the template asks for (from_field / extract_pattern /
    squash_spaces / optional - see TemplateField). Fields without any of those pass through untouched.

    A derived field takes the confidence of the words it was selected from (_confidence_of_span). When a
    pattern finds nothing, or finds an empty value, the result is empty at confidence 0 - Module 7 flags it
    - unless the field is optional and the source itself was read, in which case the blank is a result and
    keeps the source's confidence. A source that read nothing is never evidence of absence.
    """
    out: dict[str, FieldExtractionResult] = {}
    for field in fields:
        if field.from_field is None and field.extract_pattern is None and not field.squash_spaces:
            out[field.id] = read[field.id]
            continue
        source = read[field.from_field] if field.from_field is not None else read[field.id]
        text, matched, span = _select(field, source.raw_text)
        found = matched and bool(text.strip())
        source_was_read = bool(source.raw_text.strip()) and source.aggregate_confidence > 0
        if found:
            confidence, word_confidences = _confidence_of_span(source, span)
        elif field.optional and source_was_read:
            confidence, word_confidences = source.aggregate_confidence, []
        else:
            confidence, word_confidences = 0.0, []
        out[field.id] = FieldExtractionResult(
            field_id=field.id, raw_text=text, word_confidences=word_confidences,
            aggregate_confidence=confidence, psm_used=source.psm_used, source=source.source,
        )
    return out


def extract_fields_from_text_layer(
    template_id: str, version: int, layer: TextLayer, alignment: Alignment | None = None
) -> dict[str, FieldExtractionResult]:
    """
    Text-native counterpart of extract_fields: same exact-version lookup, same
    result shape, no image and no Tesseract. Module 7 consumes the result
    without knowing which path produced it.
    """
    template: TemplateOut | None = get_template_version(template_id, version)
    if template is None:
        raise ValueError(f"No template found for {template_id!r} version {version} - was it deleted after Module 5 ran?")
    fields = _fields_on_page(template, alignment)
    read = {field.id: extract_field_from_text_layer(layer, field) for field in fields if field.from_field is None}
    return _apply_field_options(fields, read)


def extract_fields(
    template_id: str, version: int, image_bytes: bytes, alignment: Alignment | None = None
) -> dict[str, FieldExtractionResult]:
    """
    Entry point. `image_bytes` is Module 2's output shape. Fetches the
    exact confirmed version, never "latest" for this doc_type - Module 5
    may have confirmed against an older version than whatever is newest by
    the time this runs.

    With an `alignment` (Module 5 found the template's printed words on this page) every box is
    read where it sits on this page rather than where the template stored it.
    """
    template: TemplateOut | None = get_template_version(template_id, version)
    if template is None:
        raise ValueError(f"No template found for {template_id!r} version {version} - was it deleted after Module 5 ran?")

    image = Image.open(io.BytesIO(image_bytes))
    if not template.fields:
        return {}
    fields = _fields_on_page(template, alignment)

    # Decode now, on this thread. Image.open is lazy and the first crop()
    # triggers the decode - so without this, several workers enter load()
    # at once on the same half-initialized object and it dies on an internal
    # assertion. Once loaded the pixel buffer is only ever read, and crop()
    # returns a fresh image per call, so the workers below share nothing
    # mutable.
    image.load()

    # Fields are read concurrently. Every OCR call is an independent Tesseract
    # subprocess over its own crop - nothing is shared, nothing is ordered -
    # so this is purely a latency change and returns byte-identical results to
    # reading them one at a time. It matters because reading each field at
    # several segmentation modes multiplied the call count: measured on a
    # 33-field air waybill, 99 calls at ~195ms each is 19.3s serially against
    # 3.2s across 8 workers. The GIL is not a factor - the work happens in
    # subprocesses, and the calling thread is blocked on I/O throughout.
    # A derived field (from_field) is selected out of another field's text afterwards: it is not read.
    to_read = [field for field in fields if field.from_field is None]
    with ThreadPoolExecutor(max_workers=FIELD_OCR_WORKERS) as pool:
        results = pool.map(lambda field: extract_field(image, field), to_read)
        read = {result.field_id: result for result in results}
    return _apply_field_options(fields, read)
