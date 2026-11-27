# -*- coding: utf-8 -*-
"""rehearse_migrate：清理必须绑定"预演专用目录"，源台账与无关文件不能被顺手删掉。"""
import pytest

from mianjing_kb import rehearse_migrate as rm
from mianjing_kb.kb import db


def _mk_db(path):
    conn = db.connect(path)
    conn.close()


def test_unrelated_files_in_work_dir_block_cleanup(tmp_path):
    src = tmp_path / "kb.sqlite"
    _mk_db(src)
    work = tmp_path / "rehearsal"
    work.mkdir()
    (work / "notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="非预演产物"):
        rm.rehearse(src, work, run_plan=False)

    assert (work / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert src.exists()


def test_work_dir_that_contains_source_db_is_refused(tmp_path):
    src = tmp_path / "kb.sqlite"
    _mk_db(src)

    with pytest.raises(ValueError, match="重叠"):
        rm.rehearse(src, tmp_path, run_plan=False)

    assert src.exists()


def test_clean_run_reports_verified_removal(tmp_path):
    src = tmp_path / "kb.sqlite"
    _mk_db(src)
    work = tmp_path / "rehearsal"

    out = rm.rehearse(src, work, run_plan=False)

    assert out["ok"] is True
    assert out["cleanup"] == "removed"
    assert not work.exists()
    assert src.exists()


def test_keep_reports_retained_copy(tmp_path):
    src = tmp_path / "kb.sqlite"
    _mk_db(src)
    work = tmp_path / "rehearsal"

    out = rm.rehearse(src, work, run_plan=False, keep=True)

    assert out["ok"] is True
    assert out["cleanup"] == "kept"
    assert (work / "kb_dry.sqlite").exists()


def test_reused_work_dir_with_only_previous_outputs_is_allowed(tmp_path):
    src = tmp_path / "kb.sqlite"
    _mk_db(src)
    work = tmp_path / "rehearsal"

    rm.rehearse(src, work, run_plan=False, keep=True)
    out = rm.rehearse(src, work, run_plan=False)

    assert out["ok"] is True
    assert out["cleanup"] == "removed"
