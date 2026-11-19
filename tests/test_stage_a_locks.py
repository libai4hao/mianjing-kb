# -*- coding: utf-8 -*-
"""阶段 A：reconvert / purge_origin 实际执行必须与 intake 共用 kb.lock。"""
from mianjing_kb import purge_origin, reconvert
from mianjing_kb.kb import config, db, wlock


def test_purge_apply_refuses_when_kb_lock_held(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    victim = root / "a.pdf"
    victim.write_bytes(b"victim")
    db_path = tmp_path / "kb.sqlite"

    with wlock.write_lock("kb", tool="intake", build_dir=db_path.parent):
        rc = purge_origin.main(["--roots", str(root), "--db", str(db_path),
                                "--apply", "--yes"])
    assert rc == 2
    assert victim.exists()
    err = capsys.readouterr().err
    assert "ABORT" in err and "intake" in err


def test_purge_dry_run_does_not_need_lock(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    db_path = tmp_path / "kb.sqlite"
    db.connect(db_path).close()
    with wlock.write_lock("kb", tool="intake", build_dir=db_path.parent):
        assert purge_origin.main(["--roots", str(root), "--db", str(db_path)]) == 0


def test_reconvert_apply_refuses_when_kb_lock_held(tmp_path, monkeypatch, capsys):
    build = tmp_path / "build"
    build.mkdir()
    (build / "convert_log.json").write_text("[]", encoding="utf-8")
    db_path = tmp_path / "kb.sqlite"
    db.connect(db_path).close()
    monkeypatch.setattr(reconvert, "BUILD", build)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    with wlock.write_lock("kb", tool="intake", build_dir=db_path.parent):
        assert reconvert.main([]) == 2
    err = capsys.readouterr().err
    assert "ABORT" in err and "intake" in err


def test_reconvert_dry_run_does_not_need_lock(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    (build / "convert_log.json").write_text("[]", encoding="utf-8")
    db_path = tmp_path / "kb.sqlite"
    db.connect(db_path).close()
    monkeypatch.setattr(reconvert, "BUILD", build)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    with wlock.write_lock("kb", tool="intake", build_dir=db_path.parent):
        assert reconvert.main(["--dry-run"]) == 0
