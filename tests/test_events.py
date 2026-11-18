"""note 结构化事件（T12）：旧 note 解析、事件幂等、读方优先事件、历史回填。"""
from tools.kb import db, events, status

DOC = "abc123abc123abcd"


def test_parse_note_new_and_legacy_formats():
    note = ("converted_to:03-数据库/丁.docx#deadbeefdeadbeef | needs_convert | "
            "replaced_by:0123456789abcdef | moved_from:31-收件箱/丁.doc")
    kinds = dict(events.parse_note(note))
    assert kinds["converted_to"] == {"dir": "03-数据库", "name": "丁.docx",
                                     "hash": "deadbeefdeadbeef"}
    assert kinds["needs_convert"] == {}
    assert kinds["replaced_by"] == {"hash": "0123456789abcdef"}
    assert kinds["moved_from"] == {"rel": "31-收件箱/丁.doc"}
    # 历史裸格式（无 #hash）：T19 需要按文件名解析
    bare = events.parse_note("converted_to:丙.docx")
    assert bare == [("converted_to", {"dir": None, "name": "丙.docx", "hash": None})]


def test_parse_note_keeps_unknown_fragment():
    assert events.parse_note("随便写的备注") == [("note_fragment", {"text": "随便写的备注"})]
    assert events.parse_note(None) == []


def test_append_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "kb.sqlite")
    assert events.append(conn, DOC, events.KIND_CONVERTED_TO, {"name": "a.docx"}) is True
    assert events.append(conn, DOC, events.KIND_CONVERTED_TO, {"name": "a.docx"}) is False
    assert events.has(conn, DOC, events.KIND_CONVERTED_TO)
    conn.close()


def test_converted_targets_prefers_events_over_note(tmp_path):
    conn = db.connect(tmp_path / "kb.sqlite")
    events.append(conn, DOC, events.KIND_CONVERTED_TO,
                  {"dir": "A", "name": "new.docx", "hash": "a" * 16})
    targets = events.converted_targets(conn, DOC, "converted_to:A/old.docx#b" * 1)
    assert targets == [{"dir": "A", "name": "new.docx", "hash": "a" * 16}]
    conn.close()


def test_backfill_legacy_notes(tmp_path):
    conn = db.connect(tmp_path / "kb.sqlite")
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, src_path, dir_path, status, "
        "batch_id, first_seen_at, last_seen_at, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (DOC, "x" * 64, "old.doc", "/old.doc", "A", status.SUPERSEDED, "b", "t", "t",
         "converted_to:old.docx | needs_convert"))
    conn.commit()
    out = events.backfill(conn)
    assert out["events_added"] == 2
    assert events.has(conn, DOC, events.KIND_CONVERTED_TO)
    assert events.has(conn, DOC, events.KIND_NEEDS_CONVERT)
    # 再跑一次不重复记
    assert events.backfill(conn)["events_added"] == 0
    conn.close()


def test_available_false_on_db_without_table(tmp_path):
    import sqlite3
    raw = sqlite3.connect(str(tmp_path / "raw.sqlite"))
    raw.close()
    conn = sqlite3.connect(str(tmp_path / "raw.sqlite"))
    assert events.available(conn) is False
    assert events.append(conn, DOC, "x") is False
    assert events.rows(conn, DOC) == []
    conn.close()


def test_parse_note_covers_every_kind_intake_writes():
    """intake 用 f"{action}_from" 造 kind，列表必须与解析表同步，否则回填会退化成 note_fragment。"""
    note = ("moved_from:A/x.pdf | renamed_from:B/y.pdf | resurrected_from:A/z.pdf | "
            "to_quarantine_from:99-重复文件待删/z.pdf | revived_from:C/w.pdf")
    kinds = [k for k, _ in events.parse_note(note)]
    assert kinds == ["moved_from", "renamed_from", "resurrected_from",
                     "to_quarantine_from", "revived_from"]
    assert "note_fragment" not in kinds
