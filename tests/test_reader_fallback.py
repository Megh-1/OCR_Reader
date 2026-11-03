"""A page the vision reader was meant to read but Tesseract had to: nothing it reads reaches `auto`."""

import app.modules.pipeline as pipeline
from app.modules.validation import FieldValidationResult, ValidationSummary, demote_auto
from app.modules.vlm_reader.client import ReaderUnavailable
from tests.test_console_api import name_field, ocr, process  # noqa: F401  (ocr is a fixture)
from tests.test_pipeline import save_template


def _unavailable(*args, **kwargs):
    raise ReaderUnavailable("the service is down")


def test_a_validated_confident_read_is_auto_when_nothing_was_expected_of_the_vision_reader(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 97)
    assert page["reader"] == "ocr" and page["result"]["fields"]["name"]["approval_tier"] == "auto"


def test_the_same_read_is_capped_at_spot_check_when_the_vision_reader_could_not_be_used(client, unique_doc_type, ocr, monkeypatch):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    monkeypatch.setattr(pipeline.vlm, "enabled", lambda template: True)
    monkeypatch.setattr(pipeline.vlm, "read_page", _unavailable)
    page = process(client, unique_doc_type, 97)

    field = page["result"]["fields"]["name"]
    assert page["reader"] == "ocr_fallback"
    assert field["approval_tier"] == "spot_check" and field["auto_basis"] is None
    assert len(page["spot_check_queue_ids"]) == 1  # a person still sees it


def test_a_rejected_field_stays_rejected_and_demotion_counts_only_what_was_auto():
    def result(field_id, tier, accepted=True):
        return FieldValidationResult(field_id=field_id, value="x", confidence=95.0, accepted=accepted, approval_tier=tier)

    summary = ValidationSummary(
        accepted={"a": result("a", "auto"), "b": result("b", "spot_check"), "c": result("c", "auto")},
        flagged={"d": result("d", "review", accepted=False)},
    )
    assert demote_auto(summary) == 2
    assert {k: v.approval_tier for k, v in summary.accepted.items()} == {"a": "spot_check", "b": "spot_check", "c": "spot_check"}
    assert summary.flagged["d"].approval_tier == "review"
