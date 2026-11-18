# -*- coding: utf-8 -*-
"""intake 的判重行为：新增 / 重复 / 改名移动 / 删除后回归。"""
import json
import sqlite3
from pathlib import Path

import pytest

from intake import main


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "corpus"
    (root / "02-JVM").mkdir(parents=True)
    (root / "02-JVM" / "JVM面试题.pdf").write_bytes(b"%PDF-1.4 fake jvm content")
    (root / "12-综合题库").mkdir(parents=True)
    (root / "12-综合题库" / "笔记.md").write_text("内容", encoding="utf-8")
    (root / "99-重复文件待删").mkdir(parents=True)
    (root / "99-重复文件待删" / "旧件.pdf").write_bytes(b"%PDF-1.4 old dup")
    return root


def rows(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out = conn.execute("SELECT * FROM source_documents").fetchall()
    conn.close()
    return out


def test_first_scan_adds_and_quarantines(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(corpus), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["added"] == 2
    assert summary["actions"]["quarantined"] == 1
    data = rows(db_path)
    assert len(data) == 3
    statuses = {r["file_name"]: r["status"] for r in data}
    assert statuses["旧件.pdf"] == "quarantined"
    assert statuses["JVM面试题.pdf"] == "pending_classify"


def test_rescan_is_idempotent(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["duplicate"] == 3
    assert summary["actions"].get("added", 0) == 0
    assert len(rows(db_path)) == 3


def test_rename_is_moved_not_new(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "02-JVM" / "JVM面试题.pdf").rename(corpus / "02-JVM" / "JVM面试题-改名.pdf")
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["renamed"] == 1
    assert len(rows(db_path)) == 3
    renamed = [r for r in rows(db_path) if r["file_name"].endswith("改名.pdf")]
    # 改名只更新路径与文件名，不能把状态降级（否则已入库成果会被"回滚"）
    assert renamed and renamed[0]["status"] == "pending_classify"


def test_deleted_then_returned_is_resurrected(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    target = corpus / "12-综合题库" / "笔记.md"
    raw = target.read_bytes()
    target.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE source_documents SET status='absent' WHERE file_name='笔记.md'")
    conn.commit()
    conn.close()

    target.write_bytes(raw)
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"]["resurrected"] == 1


def test_same_path_new_content_retires_ghost(tmp_path, capsys):
    """F7：同路径换内容（OCR 合并/懒加载改写）→ 旧行退役 superseded，不再双份抽题。

    幽灵行：intake 只按 hash 判重，同路径新内容判 added 落新行，旧行（不同 hash）
    仍 classified——doc_pool 同路径双份抽题，G5 只查 src 存在拦不住。
    """
    root = tmp_path / "corpus"
    f = root / "12-综合题库" / "笔记.md"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"version 1")
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()
    # 模拟旧行已有入库成果（classified）
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE source_documents SET status='classified'")
    conn.commit()
    conn.close()

    f.write_bytes(b"version 2 rewritten")          # 同路径、内容变了
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"]["added"] == 1
    assert summary["actions"].get("ghost_retired", 0) == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    data = conn.execute("SELECT status, note FROM source_documents").fetchall()
    conn.close()
    assert len(data) == 2
    old = [r for r in data if r["status"] == "superseded"]
    new = [r for r in data if r["status"] == "pending_classify"]
    assert len(old) == 1 and len(new) == 1
    assert old[0]["note"].startswith("replaced_by:")


def test_rescan_after_retire_is_stable(tmp_path, capsys):
    """退役后再扫：新行正常 duplicate，旧行保持 superseded 不复活也不重复退役。"""
    root = tmp_path / "corpus"
    f = root / "12-综合题库" / "笔记.md"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"version 1")
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(root), "--db", str(db_path)])
    capsys.readouterr()
    f.write_bytes(b"version 2")
    main(["--root", str(root), "--db", str(db_path)])
    capsys.readouterr()

    main(["--root", str(root), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"].get("added", 0) == 0
    assert summary["actions"].get("ghost_retired", 0) == 0
    conn = sqlite3.connect(str(db_path))
    n_super = conn.execute("SELECT COUNT(*) FROM source_documents WHERE status='superseded'").fetchone()[0]
    conn.close()
    assert n_super == 1


def test_move_onto_occupied_path_retires_ghost(tmp_path, capsys):
    """F7 补遗（评审 Important#1）：移动/改名落到已占用路径 → 占用行同样退役。

    乙的内容改落到甲的路径：hash_b 行被改指过去，甲路径上 hash_a 的占用行若不清退，
    仍 classified、src 仍存在——doc_pool 同路径双份抽题 + hash_a 绑错内容，与原地
    改内容是同一故障模式。
    """
    root = tmp_path / "corpus"
    d = root / "02-JVM"
    d.mkdir(parents=True)
    (d / "甲.pdf").write_bytes(b"%PDF-1.4 jia")
    (d / "乙.pdf").write_bytes(b"%PDF-1.4 yi")
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(root), "--db", str(db_path)])
    capsys.readouterr()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE source_documents SET status='classified'")
    conn.commit()
    conn.close()

    (d / "甲.pdf").unlink()
    (d / "乙.pdf").rename(d / "甲.pdf")          # 乙的内容落到甲的路径上
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"].get("renamed", 0) + summary["actions"].get("moved", 0) == 1
    assert summary["actions"].get("ghost_retired", 0) == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    data = conn.execute("SELECT status, note, file_name FROM source_documents").fetchall()
    conn.close()
    assert len(data) == 2
    old = [r for r in data if r["status"] == "superseded"]
    new = [r for r in data if r["status"] == "classified"]
    assert len(old) == 1 and old[0]["note"].startswith("replaced_by:")
    assert len(new) == 1 and new[0]["file_name"] == "甲.pdf"


def test_retire_preserves_existing_note(tmp_path, capsys):
    """评审 Minor#2：退役保留旧行原有 note 留痕（needs_convert 等），不整段覆盖。"""
    root = tmp_path / "corpus"
    d = root / "02-JVM"
    d.mkdir(parents=True)
    (d / "甲.pdf").write_bytes(b"%PDF-1.4 jia")
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(root), "--db", str(db_path)])
    capsys.readouterr()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE source_documents SET status='classified', note='needs_convert:1'")
    conn.commit()
    conn.close()

    (d / "甲.pdf").write_bytes(b"%PDF-1.4 jia-v2")     # 同路径换内容
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    conn = sqlite3.connect(str(db_path))
    note = conn.execute("SELECT note FROM source_documents WHERE status='superseded'").fetchone()[0]
    conn.close()
    assert note.startswith("replaced_by:")
    assert "needs_convert:1" in note


def test_dry_run_with_existing_db_does_not_write(corpus, tmp_path, capsys):
    """库已存在时 --dry-run 用只读连接，但仍要能口径一致地报重复数，且不写库（曾 crash）。"""
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "02-JVM" / "新增件.pdf").write_bytes(b"%PDF-1.4 brand new")
    assert main(["--root", str(corpus), "--db", str(db_path), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"].get("candidate", 0) == 0   # 库已存在，不是候选统计模式
    assert summary["actions"]["added"] == 1
    assert summary["actions"]["duplicate"] == 3
    assert len(rows(db_path)) == 3                     # 只读，没新增行


def test_copy_in_staging_is_duplicate_not_moved(corpus, tmp_path, capsys):
    """往收件箱丢一份库内已有文件的副本：应判 duplicate，绝不能把库内记录"搬"到收件箱。"""
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()

    (corpus / "31-收件箱").mkdir()
    (corpus / "31-收件箱" / "JVM面试题.pdf").write_bytes((corpus / "02-JVM" / "JVM面试题.pdf").read_bytes())
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["duplicate"] >= 1
    assert summary["actions"].get("moved", 0) == 0
    row = [r for r in rows(db_path) if r["file_name"] == "JVM面试题.pdf"][0]
    assert row["dir_path"] == "02-JVM"          # 原件位置没被副本夺走


def test_dry_run_writes_nothing(corpus, tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(corpus), "--db", str(db_path), "--dry-run"]) == 0
    capsys.readouterr()
    assert not db_path.exists()


def test_quarantine_copy_cannot_steal_path(tmp_path, capsys):
    """隔离区里的同内容件只算 duplicate，不允许把库内记录的路径改到 99/ 下。

    ADR D-12：曾出现 12-综合题库 的正式件被 99-重复文件待删 的同内容副本"夺主"，
    记录路径被改写、且混进了可入库清单。
    """
    root = tmp_path / "corpus"
    real = root / "12-综合题库" / "设计模式面试专题.md"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"same content")
    dup = root / "99-重复文件待删" / "12-综合题库" / "设计模式面试专题.md"
    dup.parent.mkdir(parents=True)
    dup.write_bytes(b"same content")

    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()

    from kb import hashing
    row = [r for r in rows(db_path) if r["doc_hash"] == hashing.sha256_file(real)[:16]]
    assert len(row) == 1
    assert row[0]["dir_path"] == "12-综合题库"
    assert row[0]["status"] == "pending_classify"
    assert row[0]["parent_dir"] == "12-综合题库"


def test_move_into_quarantine_flips_status(tmp_path, capsys):
    """过时件清理：正式区记录被移进 99- 区后，路径要改、状态要转 quarantined、不再进清单。"""
    root = tmp_path / "corpus"
    src = root / "05-Redis与缓存" / "memcached面试专题.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF old topic")
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()

    dest_dir = root / "99-重复文件待删" / "05-Redis与缓存"
    dest_dir.mkdir(parents=True)
    src.rename(dest_dir / "memcached面试专题.pdf")

    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["actions"]["to_quarantine"] == 1
    assert summary["actions"].get("moved", 0) == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT dir_path, status FROM source_documents WHERE file_name='memcached面试专题.pdf'").fetchone())
    in_queue = conn.execute("SELECT COUNT(*) FROM ingest_queue WHERE file_name='memcached面试专题.pdf'").fetchone()[0]
    conn.close()
    assert row["status"] == "quarantined"
    assert row["dir_path"] == "99-重复文件待删/05-Redis与缓存"
    assert in_queue == 0


def test_derived_tree_never_becomes_source(tmp_path, capsys):
    """99 区里镜像的 20-题库MD 派生件不能当新源登记（否则过时件清理会把派生 md 变成行）。"""
    root = tmp_path / "corpus"
    derived = root / "99-重复文件待删" / "20-题库MD" / "题库" / "Redis" / "memcached面试专题.md"
    derived.parent.mkdir(parents=True)
    derived.write_text("# 派生件", encoding="utf-8")
    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root), "--db", str(db_path)]) == 0
    capsys.readouterr()
    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
    conn.close()
    assert total == 0


def test_subdir_scan_keeps_corpus_prefix(tmp_path, capsys, monkeypatch):
    """只扫收件箱时也要登记 31-收件箱/ 前缀。

    否则 dir_path 丢掉前缀，folder_rules 的 31-收件箱/面经/** 结构位规则永远失配
    → 整批 771 件被判未分类（本轮实际踩过的坑）。
    """
    root = tmp_path / "corpus"
    inbox = root / "31-收件箱" / "面经"
    inbox.mkdir(parents=True)
    (inbox / "阿里面经.pdf").write_bytes(b"%PDF-1.4 alibaba mianjing")

    from kb import config
    monkeypatch.setattr(config, "CORPUS_ROOT", root)

    db_path = tmp_path / "kb.sqlite"
    assert main(["--root", str(root / "31-收件箱"), "--db", str(db_path)]) == 0
    capsys.readouterr()
    row = rows(db_path)[0]
    assert row["dir_path"] == "31-收件箱/面经"
    assert row["parent_dir"] == "面经"


def test_content_only_in_quarantine_is_revived_not_duplicate(corpus, tmp_path, capsys):
    """99 区里的副本只是待删副本；正式区再见到同内容时，内容必须回正式区重新分类，
    不能一律判 duplicate（否则"库里只有隔离副本"的件既不入库也不留痕）。"""
    db_path = tmp_path / "kb.sqlite"
    main(["--root", str(corpus), "--db", str(db_path)])
    capsys.readouterr()
    # 正式区放进一份与 99 区旧件同内容、同 hash 的文件
    (corpus / "12-综合题库" / "旧件副本.pdf").write_bytes(b"%PDF-1.4 old dup")
    main(["--root", str(corpus), "--db", str(db_path)])
    summary = json.loads(capsys.readouterr().out)

    assert summary["actions"]["revived"] == 1
    row = [r for r in rows(db_path) if r["file_name"] == "旧件副本.pdf"][0]
    assert row["status"] == "pending_classify"
    assert row["src_path"].endswith("旧件副本.pdf")
    assert row["note"].startswith("revived_from:")
    # 回到 99 区那一侧的文件仍算重复，不会把路径与状态又改回去
    main(["--root", str(corpus), "--db", str(db_path)])
    again = json.loads(capsys.readouterr().out)
    assert again["actions"].get("revived", 0) == 0
    assert [r["status"] for r in rows(db_path)
            if r["file_name"] == "旧件副本.pdf"] == ["pending_classify"]