"""
The vision reader end to end on REAL waybills, with the GPU replaced by the replies it gave in round 4.

`read_page` runs for real (preprocessing, page OCR, alignment, crops, word reader, decisions); only the service is a lookup that matches
each crop it is sent to a saved reply by the crop's exact bytes and question. A crop with no saved reply fails the test, which is how it
proves the app cuts the very windows the model read; the right and gated counts below are the ones scripts/score_vlm.py reported
(adapter wb_v4 epoch 3, train-only landmarks, the 5 held-out documents - contaminated, see CLAUDE.md).

Needs the untracked corpus (data/real_awb, data/gpu_results, data/reader_profiles); skipped without it.
"""

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app import config
from app.modules import pipeline
from app.modules.preprocessing import preprocess_image
from app.modules.vlm_reader import decide as D
from app.modules.vlm_reader import reader as R
from app.modules.vlm_reader.client import ReaderClient
from app.schemas.template import TemplateLandmark, TemplateOut

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "data" / "real_awb"
WORK = REAL / "work"
RESULTS = ROOT / "data" / "gpu_results"
PROFILES = ROOT / "data" / "reader_profiles"

pytestmark = pytest.mark.skipif(
    not (REAL.is_dir() and RESULTS.is_dir() and (PROFILES / "waybill_B.json").is_file() and (PROFILES / "waybill_M.json").is_file()
         and (RESULTS / "v4e3_B_test.jsonl").is_file()),
    reason="the untracked corpus, GPU results and reader profiles are not here",
)

# document -> (right fields, gated fields) as scripts/score_vlm.py reported them for wb_v4 epoch 3
EXPECTED_B = {"01": (27, 10), "03": (27, 14), "13": (32, 9), "14": (31, 4)}
EXPECTED_M = {"07": (30, 13)}


class SavedReplies(ReaderClient):
    """The reader service, as a lookup of what the model said to exactly this crop and question."""

    def __init__(self, form: str, evalset: str, results: str):
        super().__init__(url="http://saved.replies")
        crops = zipfile.ZipFile(WORK / evalset / "crops.zip")
        self.replies = {}
        for line in (RESULTS / results).read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            png = crops.read(e["image"].replace("\\", "/"))
            self.replies[(hashlib.sha256(png).hexdigest(), e["prompt"])] = e["reply"]

    def read(self, items, max_new=512):
        out = []
        for png, prompt in items:
            key = (hashlib.sha256(png).hexdigest(), prompt)
            assert key in self.replies, "the app cut a crop (or asked a question) the model never saw"
            out.append(self.replies[key])
        return out


def _template(form: str) -> TemplateOut:
    from scripts import seed_templates, waybill_forms
    source = waybill_forms.AIR_WAYBILL_BKK if form == "B" else seed_templates.AIR_WAYBILL
    landmarks = REAL.parent.parent / "scripts" / "landmarks" / ("air_waybill_bkk_trainonly.json" if form == "B" else "air_waybill_trainonly.json")
    data = source.model_dump()
    data["landmarks"] = [TemplateLandmark(**d) for d in json.loads(landmarks.read_text(encoding="utf-8"))]
    return TemplateOut(**{**data, "version": 1, "created_at": date.today()})


def _page(num: str):
    label_file = next((REAL / "labels").glob(f"{num}*.json"))
    label = json.loads(label_file.read_text(encoding="utf-8"))
    pdf = next(REAL.glob(f"*/{label['file']}"))
    pages, _meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
    return pages[0], preprocess_image(pages[0], deskew=True), label["fields"]


@pytest.fixture(autouse=True)
def _profiles(monkeypatch):
    monkeypatch.setattr("app.modules.vlm_reader.profile.VLM_READER_PROFILE_DIR", str(PROFILES))
    from app.modules.vlm_reader.profile import load_profile
    load_profile.cache_clear()


@pytest.mark.parametrize("form,expected,evalset,results", [
    ("B", EXPECTED_B, "evalset_v4_test_B", "v4e3_B_test.jsonl"),
    ("M", EXPECTED_M, "evalset_v4_test_M", "v4e3_M_test.jsonl"),
])
def test_read_page_reproduces_the_scored_held_out_reads(form, expected, evalset, results):
    template = _template(form)
    client = SavedReplies(form, evalset, results)
    for num, (right, gated) in expected.items():
        page_bytes, generic, labels = _page(num)
        page = R.read_page(template, page_bytes, generic, client)
        got = {fid: r.raw_text for fid, r in page.fields.items()}
        assert sum(D.norm(got[fid]) == D.norm(want) for fid, want in labels.items()) == right, num
        assert page.gated == gated == sum(r.aggregate_confidence == config.VLM_GATED_CONFIDENCE for r in page.fields.values()), num
        assert all(r.source == "vlm" for r in page.fields.values())
        # none of the gated values is wrong (the property the gate exists for)
        assert all(D.norm(got[fid]) == D.norm(labels[fid]) for fid, r in page.fields.items()
                   if r.aggregate_confidence == config.VLM_GATED_CONFIDENCE and fid in labels), num


def test_a_document_posted_to_the_api_goes_through_the_vision_reader_into_tiers_and_the_review_queue(client, unique_doc_type, monkeypatch):
    """The whole path with the database: POST /documents/process on a held-out waybill of a template that names a reader profile, the
    service answering with the saved replies. Module 7's tiers and Module 9's queue see the vision reads like any others, and
    no gated value is `auto` (the fixed gated confidence sits under the auto threshold on purpose)."""
    from app.db import SessionLocal
    from app.models.review_queue import ReviewQueueRecord
    from scripts import waybill_forms

    num = "14"
    template = _template("B")
    body = waybill_forms.AIR_WAYBILL_BKK.model_dump(mode="json")
    body.update(template_id=unique_doc_type, landmarks=[lm.model_dump() for lm in template.landmarks])
    assert client.post("/templates", json=body).status_code == 201

    monkeypatch.setattr(config, "VLM_READER_URL", "http://saved.replies")
    saved = SavedReplies("B", "evalset_v4_test_B", "v4e3_B_test.jsonl")
    monkeypatch.setattr(R.engines, "get_client", lambda name=None, transport=None: saved)

    label = json.loads(next((REAL / "labels").glob(f"{num}*.json")).read_text(encoding="utf-8"))
    pdf = next(REAL.glob(f"*/{label['file']}"))
    # a PDF with a text layer would be read from the text; the held-out page 14 is a scan, as in the experiment
    resp = client.post("/documents/process", files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")}, data={"type_hint": unique_doc_type})
    assert resp.status_code == 200, resp.text
    page = resp.json()["pages"][0]
    assert page["confirmed"] and page["reader"] == "vlm" and not page["text_native"]
    tiers = {fid: f["approval_tier"] for fid, f in page["result"]["fields"].items()}
    assert "auto" not in tiers.values()
    assert tiers and set(tiers.values()) <= {"spot_check", "review"}
    assert len(page["spot_check_queue_ids"]) == sum(t == "spot_check" for t in tiers.values())
    assert len(page["field_review_queue_ids"]) == sum(t == "review" for t in tiers.values())
    with SessionLocal() as s:
        rows = s.query(ReviewQueueRecord).filter(ReviewQueueRecord.id.in_(page["spot_check_queue_ids"])).all()
        assert {r.payload["why"] for r in rows} <= {"below_auto_threshold", "no_validator", "blank_optional"}
