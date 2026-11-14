"""Text-layer registration (app/modules/layer_registration.py): geometry plus format, no value looked at."""

import pytest

from app.models.text_layer import TextLayer, TextWord
from app.modules.layer_registration import register_boxes, shifted
from app.modules.validation import ValidationSummary, FieldValidationResult, demote_fields
from app.schemas.template import TemplateField

H = 0.012


def _field(fid: str, y: float, regex: str | None = None, x: float = 0.1, w: float = 0.12) -> TemplateField:
    return TemplateField(id=fid, label=fid, bbox=(x, y, x + w, y + H), data_type="text", validation_regex=regex, confidence_floor=0.5)


def _layer(fields, dy: float, texts) -> TextLayer:
    return TextLayer(words=tuple(TextWord(text=t, bbox=(f.bbox[0], f.bbox[1] + dy, f.bbox[0] + 0.05, f.bbox[3] + dy)) for f, t in zip(fields, texts)))


FIELDS = [_field("a", 0.30, r"[A-Z]{3}"), _field("b", 0.345, r"\d{2}-[A-Z]{3}-\d{4}", x=0.4), _field("c", 0.39, r"[A-Z]{3}", x=0.7), _field("d", 0.435, r"\d{4}", x=0.1)]
TEXTS = ["DXB", "17-NOV-2026", "BKK", "1234"]


def test_a_layer_typed_off_its_cells_is_found_and_the_boxes_follow():
    layer = _layer(FIELDS, 0.0225, TEXTS)
    shifts = register_boxes(layer, FIELDS)
    assert set(shifts) == {f.id for f in FIELDS}
    for fid, (dx, dy) in shifts.items():
        assert abs(dy - 0.0225) <= 0.0025 and abs(dx) <= 0.005, fid


def test_a_layer_already_on_its_cells_moves_nothing():
    layer = _layer(FIELDS, 0.0, TEXTS)
    assert register_boxes(layer, FIELDS) == {}


def test_too_few_fields_or_no_words_register_nothing():
    assert register_boxes(_layer(FIELDS[:2], 0.02, TEXTS[:2]), FIELDS[:2]) == {}
    assert register_boxes(TextLayer(words=()), FIELDS) == {}


def test_a_gain_under_the_minimum_is_not_applied(monkeypatch):
    import app.modules.layer_registration as lr
    monkeypatch.setattr(lr, "TEXT_LAYER_REG_MIN_GAIN", 1000.0)
    assert register_boxes(_layer(FIELDS, 0.0225, TEXTS), FIELDS) == {}


def test_shifted_moves_a_box_by_the_shift():
    assert shifted((0.1, 0.2, 0.3, 0.4), (0.01, -0.02)) == pytest.approx((0.11, 0.18, 0.31, 0.38))


def test_demote_fields_caps_only_the_named_auto_fields():
    def r(fid, tier):
        return FieldValidationResult(field_id=fid, value="x", confidence=100.0, accepted=True, approval_tier=tier)

    summary = ValidationSummary(accepted={"a": r("a", "auto"), "b": r("b", "auto"), "c": r("c", "spot_check")}, flagged={})
    assert demote_fields(summary, {"a", "c", "zzz"}) == 1
    assert [summary.accepted[k].approval_tier for k in "abc"] == ["spot_check", "auto", "spot_check"]
