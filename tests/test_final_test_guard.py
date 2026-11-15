"""scripts/run_final_test.py runs only on verified labels and only once; it opens no test file otherwise."""

import scripts.run_final_test as final


def test_it_waits_for_verified_labels(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(final, "TEST", tmp_path)
    assert final.main() == 1
    assert "waiting for verified labels" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_a_spent_split_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "LABELS_VERIFIED").write_text("x")
    (tmp_path / "SPENT").write_text("2026-11-16 abc")
    monkeypatch.setattr(final, "TEST", tmp_path)
    assert final.main() == 1
    assert "already spent" in capsys.readouterr().out


def test_the_four_configurations_asked_for():
    names = [n for n, _ in final.configurations()]
    assert names == ["current app configuration", "default configuration", "page_qwen3vl alone", "page_glmocr alone"]
