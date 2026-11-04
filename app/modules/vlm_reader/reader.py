"""
Module 6, vision-model path: one scanned or photographed page of a template that names a reader profile, read by the vision model.

    page -> one whole-page OCR (rules erased) -> landmark alignment -> a window per field at three widenings -> the reader service
         -> staged answers (decide.py) compared with the word reader (words.py) -> one FieldExtractionResult per field

It returns what Module 6 returns, so Module 7 validates it like any other read and the tiers follow. The model reports no confidence;
a value gets a fixed one by what was established about it (config.VLM_GATED_CONFIDENCE / VLM_UNGATED_CONFIDENCE), which is how it
reaches a tier. A page it cannot read raises `ReaderUnavailable` and the caller reads the page the ordinary way.

A text-native PDF page comes here only when the pipeline found its text-layer read doubtful (pipeline.extract_page_fields): then the
page's exact words stand in for the page-level OCR as the second reader, and the model still reads the rendered crops.
"""

from __future__ import annotations

import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from app import config
from app.modules import alignment as A
from app.modules.field_extraction import FieldExtractionResult
from app.modules.preprocessing import ProcessedImage, preprocess_image
from app.models.text_layer import TextLayer
from app.modules.vlm_reader import crops as C
from app.modules.vlm_reader import waybill as W
from app.modules.vlm_reader import words as K
from app.modules.vlm_reader import engines
from app.modules.vlm_reader.client import ReaderUnavailable
from app.modules.vlm_reader.decide import Decision, answer_fields, decide
from app.modules.vlm_reader.profile import ProfileMissing, load_profile
from app.schemas.template import TemplateOut

log = logging.getLogger(__name__)

MAX_NEW_TOKENS = 512  # 200 cut a long goods list mid-JSON and three fields came back blank


@dataclass
class VlmPage:
    fields: dict[str, FieldExtractionResult]
    alignment: A.Alignment
    gated: int  # how many fields passed the gate (two readers agree, or an arithmetic check holds, and the format passes)
    engine: str = "default"  # the registry engine whose model read the crops


def enabled(template: TemplateOut) -> bool:
    return bool(template.hints.vlm_reader and engines.available())


def _image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def align_from_tokens(template: TemplateOut, tokens: list[K.Tok]) -> Optional[A.Alignment]:
    words = [A.PageWord(K.key(t[0]), t[1], (t[2] + t[4]) / 2) for t in K.clean(tokens)
             if len(re.sub(r"[^A-Za-z]", "", t[0])) >= 4 and t[5] >= 30]
    return A.fit_alignment(template.landmarks, words)


def to_results(template: TemplateOut, decisions: dict[str, Decision]) -> dict[str, FieldExtractionResult]:
    """The decisions as Module 6 results. A blank is rejected (confidence 0) unless the field is optional."""
    out: dict[str, FieldExtractionResult] = {}
    for field in template.fields:
        d = decisions[field.id]
        if not d.value.strip():
            confidence = config.VLM_UNGATED_CONFIDENCE if field.optional else 0.0
        else:
            confidence = config.VLM_GATED_CONFIDENCE if d.gated else config.VLM_UNGATED_CONFIDENCE
        out[field.id] = FieldExtractionResult(field_id=field.id, raw_text=d.value, word_confidences=[], aggregate_confidence=confidence, psm_used=0, source="vlm")
    return out


def layer_tokens(layer: TextLayer) -> list[K.Tok]:
    """A PDF page's own words as word-reader tokens: exact, so a confidence of 100."""
    return [(w.text, *w.bbox, 100.0) for w in layer.words if w.text.strip()]


def read_page(template: TemplateOut, page_bytes: bytes, generic: ProcessedImage, client: Optional[engines.CropReader] = None,
              text_layer: Optional[TextLayer] = None, verified: Optional[A.Alignment] = None) -> VlmPage:
    """Reads one raster page. Raises ReaderUnavailable for anything that stops the vision reader from giving a complete answer.

    With a `text_layer` (the page is a PDF with real embedded text) the page is a render, upright by construction, so it is not
    deskewed, and the layer's words are the word reader's input instead of a page OCR. The alignment and the windows still come from
    the raster: the form's printed labels are vector art and are not in the layer."""
    name = template.hints.vlm_reader
    if not name:
        raise ReaderUnavailable("the template names no reader profile")
    client = client or engines.get_client()  # raises ReaderUnavailable when no engine is set up
    if not client.configured:
        raise ReaderUnavailable(f"reader engine {engines.engine_name(client)!r} is not set up")
    fields = {f.id: f for f in template.fields}
    if not W.FIELD_IDS <= set(fields):
        raise ReaderUnavailable(f"template {template.template_id} lacks waybill fields: {sorted(W.FIELD_IDS - set(fields))}")
    if not template.landmarks:
        raise ReaderUnavailable("the template has no landmarks to align the page with")
    try:
        profile = load_profile(name)
    except ProfileMissing as exc:
        raise ReaderUnavailable(str(exc)) from exc

    # The windows are cut from the page as scanned (generic pass); the alignment and the word reader use the pass with the form's
    # printed rules erased, so a rule is not read as a character - the two renderings the measurements were made with.
    page = _image(generic.field_image_bytes)
    erased = _image(preprocess_image(page_bytes, remove_rules=True, deskew=text_layer is None).field_image_bytes)
    tokens = K.page_tokens(erased)
    al = align_from_tokens(template, tokens)
    if al is None and verified is not None:
        # The rules-erased pass can leave a noisy scan or a photograph with almost no readable words (0-9 on six of the eight hard
        # pages measured), so there is nothing to refit. The alignment Module 5 confirmed the page with came from the same frame
        # (the generic pass; same size) and agreed on >= ALIGN_MIN_LANDMARKS words, so the windows are cut with it. Fewer words then
        # reach the word reader, which only means fewer values are gated.
        al = verified
    if al is None:
        raise ReaderUnavailable("the page could not be aligned to the template's landmarks")

    crops = C.crops_for_page(page, al, profile, fields)
    replies = client.read([(c.png(), W.prompt_for(c.kind, c.label, c.captions, c.hint)) for c in crops], max_new=MAX_NEW_TOKENS)

    answers: dict[str, dict[int, str]] = defaultdict(dict)  # field -> window level -> the model's raw answer
    for crop, reply in zip(crops, replies):
        for fid, value in answer_fields(crop.kind, crop.fields, reply).items():
            answers[fid][crop.level] = value
    wordsel = K.read_all(fields, layer_tokens(text_layer) if text_layer is not None else tokens, al, K.printed_words(profile))
    decisions = decide(fields, answers, wordsel)
    engine = engines.engine_name(client)
    log.info("vision reader %s via engine %s: %d fields, %d gated, %d blank; alignment %s", name, engine, len(decisions),
             sum(d.gated for d in decisions.values()), sum(not d.value for d in decisions.values()), al.summary())
    return VlmPage(to_results(template, decisions), al, sum(d.gated for d in decisions.values()), engine)
