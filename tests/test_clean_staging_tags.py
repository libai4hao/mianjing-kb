# -*- coding: utf-8 -*-
"""staging 标签清洗脚本：口径必须与线上 normalize_tags 一致（同义/去重/大小写维度词）。"""
import importlib.util
import json
from pathlib import Path

import pytest
from mianjing_kb.kb import config, db

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "clean_staging_tags", ROOT / "scripts" / "clean_staging_tags.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    c = db.connect(tmp_path / "kb.sqlite")
    rows = [
        (1, json.dumps(["ZooKeeper", "选主", "选主"], ensure_ascii=False)),
        (2, json.dumps(["HashMap", "集合", "扩容"], ensure_ascii=False)),
        (3, json.dumps(["a", "b", "c", "d", "e"], ensure_ascii=False)),
    ]
    for qid, tags in rows:
        c.execute("INSERT INTO labels_staging (qid, q_norm_hash, topic, qtype, difficulty, "
                  "tags, provider, model, redacted, latency_ms, tokens, labeled_at) "
                  "VALUES (?,?,'Java基础','八股',3,?,'p','m',0,1,1,'now')", (qid, f"h{qid}", tags))
    c.commit()
    yield c
    c.close()


def test_clean_drops_case_variant_and_dedups(conn):
    cst = _load()
    out = cst.clean(conn, apply=True)
    assert out["dropped_dimension"] == 1          # ZooKeeper（词表是 Zookeeper）
    assert out["deduped"] == 2                    # 选主×2 合一 + HashMap 归一后与 集合 撞车
    assert out["synonym_hits"] == 1               # HashMap -> 集合
    assert out["truncated"] == 1                  # 5 -> 4
    t1 = json.loads(conn.execute("SELECT tags FROM labels_staging WHERE qid=1").fetchone()[0])
    assert t1 == ["选主"]
    t2 = json.loads(conn.execute("SELECT tags FROM labels_staging WHERE qid=2").fetchone()[0])
    assert t2 == ["集合", "扩容"]                 # 同义归一 + 去重后只剩一个写法


def test_clean_dry_run_does_not_write(conn):
    cst = _load()
    before = conn.execute("SELECT tags FROM labels_staging WHERE qid=1").fetchone()[0]
    cst.clean(conn, apply=False)
    after = conn.execute("SELECT tags FROM labels_staging WHERE qid=1").fetchone()[0]
    assert before == after


def _main_db(tmp_path, monkeypatch):
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(config, "BUILD_DIR", configured)
    path = tmp_path / "custom" / "kb.sqlite"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO labels_staging (qid,q_norm_hash,topic,qtype,difficulty,tags,provider,"
        "model,redacted,latency_ms,tokens,labeled_at) VALUES (1,'h','Java基础','八股',3,?,'p','m',0,1,1,'now')",
        (json.dumps(["ZooKeeper", "选主", "选主"], ensure_ascii=False),))
    conn.commit()
    conn.close()
    return path


def test_main_preview_is_readonly_and_apply_locks_and_backs_up(tmp_path, monkeypatch, capsys):
    path = _main_db(tmp_path, monkeypatch)
    cst = _load()
    real_connect, real_lock, real_backup = cst.db.connect, cst.wlock.write_lock, cst.db.backup_db
    seen = []

    def checked_connect(db_path, **kwargs):
        seen.append(kwargs.get("readonly"))
        return real_connect(db_path, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("预览不应拿写锁或做备份")

    monkeypatch.setattr(cst.db, "connect", checked_connect)
    monkeypatch.setattr(cst.wlock, "write_lock", forbidden)
    monkeypatch.setattr(cst.db, "backup_db", forbidden)
    assert cst.main(["--db", str(path)]) == 0
    capsys.readouterr()
    assert seen == [True]
    assert not (config.BUILD_DIR / "locks").exists()

    seen.clear()
    monkeypatch.setattr(cst.wlock, "write_lock", real_lock)

    def checked_backup(db_path, *args, **kwargs):
        locked, holder = cst.wlock.inspect_lock(path.parent / "locks" / "kb.lock")
        seen.append((locked, (holder or {}).get("tool")))
        return real_backup(db_path, *args, **kwargs)

    monkeypatch.setattr(cst.db, "backup_db", checked_backup)
    assert cst.main(["--db", str(path), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert seen == [False, (True, "clean_staging_tags")]
    assert Path(out["backup"]).is_file()
    assert not (config.BUILD_DIR / "locks").exists()
    conn = db.connect(path, create=False, readonly=True)
    assert json.loads(conn.execute("SELECT tags FROM labels_staging").fetchone()[0]) == ["选主"]
    conn.close()


def test_main_apply_rolls_back_all_updates_on_failure(tmp_path, monkeypatch):
    path = _main_db(tmp_path, monkeypatch)
    cst = _load()
    real_clean = cst.clean

    def failing_clean(conn, apply=False):
        real_clean(conn, apply=apply)
        raise RuntimeError("模拟写入后失败")

    monkeypatch.setattr(cst, "clean", failing_clean)
    with pytest.raises(RuntimeError, match="模拟写入后失败"):
        cst.main(["--db", str(path), "--apply"])
    conn = db.connect(path, create=False, readonly=True)
    tags = json.loads(conn.execute("SELECT tags FROM labels_staging").fetchone()[0])
    conn.close()
    assert tags == ["ZooKeeper", "选主", "选主"]
