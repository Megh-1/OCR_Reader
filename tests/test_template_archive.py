"""
Soft-delete for templates. Templates are append-only, so "remove" means "stop
Module 5 scanning it", never "delete the row" - past extractions must stay
reproducible against the exact version that produced them.
"""

import io
from unittest.mock import Mock

from PIL import Image

import app.modules.type_identification as type_identification
from app.db import SessionLocal
from app.models.template import TemplateRecord
from app.modules.template_store import (
    get_latest_template,
    get_template_version,
    list_known_doc_types,
    set_template_archived,
)
from app.modules.type_identification import identify_document_type
from scripts.prune_dev_templates import Candidate, SEEDED_IDS, find_candidates
from scripts.seed_templates import AIR_WAYBILL, seed


def save_template(client, template_id):
    resp = client.post(
        "/templates",
        json={
            "template_id": template_id,
            "anchors": [{"text": "HEADER", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85}],
            "fields": [],
        },
    )
    assert resp.status_code == 201
    return resp.json()


def blank_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color=255).save(buf, format="PNG")
    return buf.getvalue()


def test_archived_doc_type_leaves_the_scan_but_stays_readable(client, unique_doc_type, monkeypatch):
    save_template(client, unique_doc_type)
    before = get_template_version(unique_doc_type, 1).model_dump()
    assert unique_doc_type in list_known_doc_types()

    assert set_template_archived(unique_doc_type, True) == 1

    assert unique_doc_type not in list_known_doc_types()
    # Append-only still holds for CONTENT: the archived version reads back identically...
    assert get_template_version(unique_doc_type, 1).model_dump() == before
    # ...and a type_hint naming it still resolves. Archiving retires it from
    # the no-hint scan; it does not make the template unreadable.
    assert get_latest_template(unique_doc_type) is not None

    # The property that matters: Module 5 really does not score it.
    scored = []
    real_check = type_identification._check_template

    def recording_check(image, template):
        scored.append(template.template_id)
        return real_check(image, template)

    monkeypatch.setattr(type_identification, "_check_template", recording_check)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value=""))

    identify_document_type(blank_png())

    assert unique_doc_type not in scored


def test_restoring_returns_it_to_the_scan(client, unique_doc_type):
    save_template(client, unique_doc_type)
    set_template_archived(unique_doc_type, True)
    assert unique_doc_type not in list_known_doc_types()

    assert set_template_archived(unique_doc_type, False) == 1
    assert unique_doc_type in list_known_doc_types()


def test_saving_a_new_version_unarchives_the_type_without_touching_the_old_row(client, unique_doc_type):
    save_template(client, unique_doc_type)
    set_template_archived(unique_doc_type, True)

    save_template(client, unique_doc_type)  # v2

    assert unique_doc_type in list_known_doc_types()
    with SessionLocal() as session:
        rows = session.query(TemplateRecord).filter_by(doc_type=unique_doc_type).order_by(TemplateRecord.version).all()
        assert [(r.version, r.is_archived) for r in rows] == [(1, True), (2, False)]


def test_archiving_an_unknown_doc_type_changes_nothing(unique_doc_type):
    assert set_template_archived(unique_doc_type, True) == 0


def test_archiving_is_idempotent(client, unique_doc_type):
    save_template(client, unique_doc_type)
    assert set_template_archived(unique_doc_type, True) == 1
    assert set_template_archived(unique_doc_type, True) == 0


# --- scripts/prune_dev_templates.py ------------------------------------------


def test_seeded_templates_are_never_prune_candidates(client, unique_doc_type):
    seed([AIR_WAYBILL])
    save_template(client, unique_doc_type)

    with SessionLocal() as session:
        candidates, _ = find_candidates(session)

    ids = {c.doc_type for c in candidates}
    assert "air_waybill" in SEEDED_IDS
    assert "air_waybill" not in ids
    assert unique_doc_type in ids


def test_keep_excludes_a_doc_type_from_candidates(client, unique_doc_type):
    save_template(client, unique_doc_type)

    with SessionLocal() as session:
        candidates, _ = find_candidates(session, keep=frozenset({unique_doc_type}))

    assert unique_doc_type not in {c.doc_type for c in candidates}


def test_archived_types_are_no_longer_candidates_and_are_counted(client, unique_doc_type):
    save_template(client, unique_doc_type)
    with SessionLocal() as session:
        _, archived_before = find_candidates(session)

    set_template_archived(unique_doc_type, True)

    with SessionLocal() as session:
        candidates, archived_after = find_candidates(session)
    assert unique_doc_type not in {c.doc_type for c in candidates}
    assert archived_after == archived_before + 1


def test_generated_names_are_separated_from_hand_named_ones():
    def candidate(name):
        return Candidate(name, 1, 1, None, 1, 0, 0)

    assert candidate("test_doc_" + "a" * 32).generated
    assert candidate("doc_a_" + "0" * 32).generated
    assert candidate("doc_b_" + "f" * 32).generated
    assert candidate("drained_14").generated
    # Named by a person - must be shown individually for review, not lumped in.
    assert not candidate("accuracy_harness_demo").generated
    assert not candidate("review_ui_test_doc").generated
    assert not candidate("some_hint").generated
