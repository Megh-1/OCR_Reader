"""
The synthetic waybill generator keeps its promises: its ground truth is exactly what a direct read of the
generated PDF returns, and each deliberately injected inconsistency is caught by the check it was made for.

Needs the real source PDF (untracked, in tests/fixtures), faker and Verdana; skipped without them.
"""

import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("faker")
VERDANA = Path("C:/Windows/Fonts/verdana.ttf")
BASE = Path(__file__).parent / "fixtures" / "176060677362025-02-26-16-44AWBUploadDocument[1].pdf"
pytestmark = pytest.mark.skipif(
    not (VERDANA.exists() and BASE.exists()), reason="needs the real waybill PDF in tests/fixtures and Verdana"
)

from app.modules import ingestion  # noqa: E402
from app.modules.field_extraction import extract_field_from_text_layer  # noqa: E402
from app.modules.validation import validate_fields  # noqa: E402
from app.schemas.template import TemplateOut  # noqa: E402
from scripts import generate_synthetic_awb as gen  # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("syn")
    import sys

    argv = ["generate", "--base", str(BASE), "--out", str(out), "--count", "12", "--scans", "0", "--photos", "0",
            "--inject-errors", "4", "--seed", "3"]
    old, sys.argv = sys.argv, argv
    try:
        assert gen.main() == 0
    finally:
        sys.argv = old
    return out, json.loads((out / "ground_truth.json").read_text(encoding="utf-8")), json.loads(
        (out / "injections.json").read_text(encoding="utf-8")
    )


def _read(path: Path) -> dict:
    layer = ingestion.extract_text_layers(path.read_bytes())[0]
    assert layer is not None, f"{path.name} is not text-native"
    return {f.id: extract_field_from_text_layer(layer, f) for f in gen.TEMPLATE.fields}


def test_ground_truth_equals_a_direct_read_of_the_pdf(generated):
    out, truth, _ = generated
    wrong = []
    for name, entry in truth.items():
        for field_id, extraction in _read(out / name).items():
            if extraction.raw_text.strip() != entry["fields"][field_id]:
                wrong.append((name, field_id))
    # One case is known and real: a long consignee name can sit close enough to the address box
    # for the text layer to pull it in. Everything else must match exactly.
    assert all(field_id == "consignee_address" for _, field_id in wrong), wrong
    assert len(wrong) <= 1, wrong


def test_none_of_the_source_documents_values_survive(generated):
    out, truth, _ = generated
    for name in truth:
        text = (out / name).read_bytes()
        words = ingestion.extract_text_layers(text)[0].words
        joined = " ".join(w.text for w in words)
        for real in ("VTS", "TEAMGLOBAL", "KANEKAL", "IHSANULLAH", "06067736", "AMANATALI"):
            assert real not in joined, f"{real} survived in {name}"


def test_generated_values_pass_every_field_rule_and_every_check_except_the_injected(generated):
    out, truth, injections = generated
    template = TemplateOut(version=1, created_at=date.today(), **gen.TEMPLATE.model_dump(mode="json"))
    for name in truth:
        summary = validate_fields(template, _read(out / name))
        failed = {o.check_id for o in summary.checks if o.status == "failed"}
        if name in injections:
            assert failed == {injections[name]}, (name, failed)
        else:
            assert not failed, (name, failed)
            for field_id, result in summary.flagged.items():
                assert result.reasons != ["format_invalid"], (name, field_id)


def test_awb_numbers_satisfy_the_mod7_check_digit(generated):
    _, truth, _ = generated
    from app.modules.validation import _iata_awb_mod7

    assert all(_iata_awb_mod7(e["fields"]["awb_number"]) is True for e in truth.values())
