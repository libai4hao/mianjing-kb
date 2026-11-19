# -*- coding: utf-8 -*-
"""只读统计：全量重建后的 kb.sqlite 基线（输出 JSON，供 docs/29 引用）。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config  # noqa: E402


def scalar(con, sql, *params):
    return con.execute(sql, params).fetchone()[0]


def main() -> int:
    db = Path(config.DB_PATH)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {"db": str(db), "size_bytes": db.stat().st_size}

    out["source_documents"] = {
        "total": scalar(con, "SELECT COUNT(*) FROM source_documents"),
        "status": dict(con.execute("SELECT status, COUNT(*) FROM source_documents GROUP BY 1").fetchall()),
        "gate_result_nonempty": scalar(con, "SELECT COUNT(*) FROM source_documents WHERE gate_result IS NOT NULL AND gate_result<>''"),
        "collections_nonempty": scalar(con, "SELECT COUNT(DISTINCT collection) FROM source_documents WHERE collection IS NOT NULL AND TRIM(collection)<>''"),
        "collection_null": scalar(con, "SELECT COUNT(*) FROM source_documents WHERE collection IS NULL OR TRIM(collection)=''"),
    }
    out["schema"] = {
        "migrations": [r[0] for r in con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()],
        "migrations_count": scalar(con, "SELECT COUNT(*) FROM schema_version"),
    }
    out["questions"] = {
        "rows": scalar(con, "SELECT COUNT(*) FROM questions"),
        "doc_hashes": scalar(con, "SELECT COUNT(DISTINCT doc_hash) FROM questions"),
        "doc_hashes_not_in_ledger": scalar(con,
            "SELECT COUNT(DISTINCT doc_hash) FROM questions WHERE doc_hash NOT IN (SELECT doc_hash FROM source_documents)"),
        "q_norm_hashes": scalar(con, "SELECT COUNT(DISTINCT q_norm_hash) FROM questions"),
        "empty_answer": scalar(con, "SELECT COUNT(*) FROM questions WHERE a_text=''"),
        "answer_lt20": scalar(con, "SELECT COUNT(*) FROM questions WHERE LENGTH(a_text)<20"),
        "dup_groups_same_doc": scalar(con,
            "SELECT COUNT(*) FROM (SELECT doc_hash, q_norm_hash FROM questions GROUP BY 1,2 HAVING COUNT(*)>1)"),
        "qtype": dict(con.execute("SELECT qtype, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "conf": dict(con.execute("SELECT conf, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
    }
    out["canonical"] = {
        "rows": scalar(con, "SELECT COUNT(*) FROM canonical_questions"),
        "witness_ge2": scalar(con, "SELECT COUNT(*) FROM canonical_questions WHERE witness>=2"),
        "witness_eq1": scalar(con, "SELECT COUNT(*) FROM canonical_questions WHERE witness<=1"),
        "variants": scalar(con, "SELECT COUNT(*) FROM question_variants"),
        "collections_in_questions": scalar(con,
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(sd.collection),''), 'doc:'||q.doc_hash)) "
            "FROM questions q JOIN source_documents sd ON sd.doc_hash=q.doc_hash"),
        "top": [dict(r) for r in con.execute(
            "SELECT c.title_norm, c.witness, "
            "(SELECT COUNT(*) FROM question_variants v WHERE v.canonical_id=c.canonical_id) AS n_variants "
            "FROM canonical_questions c ORDER BY c.witness DESC, n_variants DESC LIMIT 10")],
    }
    out["products"] = {
        "rows": scalar(con, "SELECT COUNT(*) FROM document_products"),
        "text_source": dict(con.execute("SELECT text_source, COUNT(*) FROM document_products GROUP BY 1").fetchall()),
        "extracted": scalar(con, "SELECT COUNT(*) FROM document_products WHERE extracted_at IS NOT NULL"),
        "indexed": scalar(con, "SELECT COUNT(*) FROM document_products WHERE indexed_at IS NOT NULL"),
        "questions": scalar(con, "SELECT SUM(questions_n) FROM document_products"),
        "zero_question": scalar(con, "SELECT COUNT(*) FROM document_products WHERE questions_n=0"),
    }
    out["other"] = {
        "gate_results": scalar(con, "SELECT COUNT(*) FROM gate_results"),
        "document_events": scalar(con, "SELECT COUNT(*) FROM document_events"),
        "labels_staging": scalar(con, "SELECT COUNT(*) FROM labels_staging"),
        "label_facts": scalar(con, "SELECT COUNT(*) FROM label_facts"),
        "golden_labels": scalar(con, "SELECT COUNT(*) FROM golden_labels"),
    }
    con.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
