# -*- coding: utf-8 -*-
"""SQLite 连接与迁移。表结构见 meta/schema/001~009（docs/03 §10.2）。

迁移口径（2026-11-19 统一，替换原先"SQL 文件 + db.py 里一串 _ensure_*"两套逻辑）：
- 唯一入口是 `MIGRATIONS` 有序注册表，每一项是 (版本号, 幂等函数)；
- 已应用的版本记在 `schema_version(version, applied_at)`；
- SQL 文件只是函数的载荷；006（questions 题级 topic）没有独立 SQL，就在代码里实现，
  版本号照记，不再出现"006 找不到迁移文件"的歧义。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA_FILE = Path(config.META_DIR) / "schema" / "001_init.sql"
NOW_FMT = "%Y-%m-%dT%H:%M:%S"
BACKUP_KEEP = 5
COUNT_FIELDS = {"status", "category", "collection", "source_trust", "topic",
                "sub_topic", "ext", "parent_dir", "batch_id", "gate_result"}


def connect(db_path: Path | str | None = None, *, create: bool = True,
            readonly: bool = False) -> sqlite3.Connection:
    """连接数据库；create=True 时自动建库并跑迁移。

    readonly=True 以 `mode=ro` 打开（不建库、不迁移、不写 WAL）：只读工具（如 purge_origin）
    用它在"只查询、绝不改库"的场景下工作。
    create=False 且文件不存在时直接报错——sqlite3.connect 默认会新建空库，
    参数写错就会悄悄生成一个空文件，直到"表不存在"才暴露。
    """
    path = Path(db_path or config.DB_PATH)
    if not path.exists() and not create:
        raise FileNotFoundError(f"数据库不存在：{path}")
    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"数据库不存在：{path}")
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")   # kb.sqlite 是事实源，断电不丢已提交事务
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if create:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """按 schema_version 逐个应用未落地的迁移（每项都幂等）。"""
    _apply_migrations(conn)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _ensure_parent_dir(conn: sqlite3.Connection) -> None:
    """补 parent_dir（父目录名）字段 + 可入库清单视图（002）。"""
    if not _column_exists(conn, "source_documents", "parent_dir"):
        conn.execute("ALTER TABLE source_documents ADD COLUMN parent_dir TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_parent ON source_documents(parent_dir)")
    # 注意：本迁移已上线，视图定义不得再改；口径要演进必须新增 010 迁移。
    conn.execute("DROP VIEW IF EXISTS ingest_queue")
    conn.execute(
        """
        CREATE VIEW ingest_queue AS
        SELECT doc_hash, file_name, parent_dir, dir_path, ext, size_bytes,
               category, topic, sub_topic, collection, source_trust
        FROM source_documents
        WHERE status = 'classified'
          AND dir_path NOT LIKE '99-重复文件待删%'
        """
    )


def _ensure_source_spans(conn: sqlite3.Connection) -> None:
    """补合并版"源边界"字段（003）：source_count / sources_json（见 meta/schema/003）。"""
    for col, decl in (("source_count", "INTEGER"), ("sources_json", "TEXT")):
        if not _column_exists(conn, "source_documents", col):
            conn.execute(f"ALTER TABLE source_documents ADD COLUMN {col} {decl}")


def _ensure_questions(conn: sqlite3.Connection) -> None:
    """补 questions 表 + 频次视图（004，P1-1 extract 落表）。"""
    sql = (SCHEMA_FILE.parent / "004_questions.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


def _ensure_labels_staging(conn: sqlite3.Connection) -> None:
    """补 labels_staging 打标暂存表（005，docs/15 §3.6——G6 通过才 merge）。"""
    sql = (SCHEMA_FILE.parent / "005_labels_staging.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


def _ensure_question_topic(conn: sqlite3.Connection) -> None:
    """006：questions 加题级 topic 列（LLM 打标的事实源落点，2026-11-17）。

    题级 topic 优先于文档级（build_db 重建口径：COALESCE(q.topic, sd.topic, 镜像继承)）；
    extract 落表不填（NULL），batch_label merge 填。
    """
    if not _column_exists(conn, "questions", "topic"):
        conn.execute("ALTER TABLE questions ADD COLUMN topic TEXT")


def _ensure_label_facts(conn: sqlite3.Connection) -> None:
    """007：label_facts 打标事实源 + staging/questions 伴随列（B1 修复，2026-11-19）。

    label_facts 以 q_norm_hash 为主键——重 extract 会换 qid（AUTOINCREMENT 高水位
    续号），打标事实按题干 hash 存活，extract 重切后按 hash 重挂；
    labels_staging 加 q_norm_hash 供 merge 校验题干一致性（历史 NULL 行放行）；
    questions 加 difficulty/tags（qtype 004 已有，merge 四列落库用）。
    """
    sql = (SCHEMA_FILE.parent / "007_label_facts.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    if not _column_exists(conn, "labels_staging", "q_norm_hash"):
        conn.execute("ALTER TABLE labels_staging ADD COLUMN q_norm_hash TEXT")
    for col in ("prompt_tokens", "completion_tokens"):   # F5：in/out 拆分计价（bug4）
        if not _column_exists(conn, "labels_staging", col):
            conn.execute(f"ALTER TABLE labels_staging ADD COLUMN {col} INTEGER")
    for col, decl in (("difficulty", "INTEGER"), ("tags", "TEXT")):
        if not _column_exists(conn, "questions", col):
            conn.execute(f"ALTER TABLE questions ADD COLUMN {col} {decl}")


def _ensure_golden_labels(conn: sqlite3.Connection) -> None:
    """008：golden_labels 金标表（批次二 G1，G6b 门禁依据，2026-11-19）。

    键 = q_norm_hash（与 label_facts 同源，跨 extract 重切存活）；
    人工抽检经 batch_label --golden-sample/--golden-load 录入。
    """
    sql = (SCHEMA_FILE.parent / "008_golden_labels.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


def _ensure_review_columns(conn: sqlite3.Connection) -> None:
    """009：labels_staging 加复核三列（批次三 R1，2026-11-19）。

    逐列判存在：旧实现整脚本跳过一次后，中途失败会留下"只加了一列"的库，
    下次启动看到 review_state 就跳过，另外两列永久缺失。
    """
    for col in ("review_state", "review_model", "review_note"):
        if not _column_exists(conn, "labels_staging", col):
            conn.execute(f"ALTER TABLE labels_staging ADD COLUMN {col} TEXT")


def _init_schema(conn: sqlite3.Connection) -> None:
    """001：基础表（source_documents / batches 等）。脚本本身 IF NOT EXISTS，可直接重放。"""
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


MIGRATIONS = [
    ("001_init", _init_schema),
    ("002_parent_dir", _ensure_parent_dir),
    ("003_source_spans", _ensure_source_spans),
    ("004_questions", _ensure_questions),
    ("005_labels_staging", _ensure_labels_staging),
    ("006_questions_topic", _ensure_question_topic),   # 代码内迁移，无独立 .sql（见模块 docstring）
    ("007_label_facts", _ensure_label_facts),
    ("008_golden_labels", _ensure_golden_labels),
    ("009_review_columns", _ensure_review_columns),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version").fetchall()}
    for version, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute("INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                     (version, datetime.now().strftime(NOW_FMT)))
    conn.commit()
    _validate_schema(conn)


REQUIRED = {
    "source_documents": ("doc_hash", "sha256", "src_path", "dir_path", "status",
                         "parent_dir", "source_count", "sources_json"),
    "questions": ("qid", "doc_hash", "topic", "difficulty", "tags"),
    "labels_staging": ("q_norm_hash", "prompt_tokens", "completion_tokens",
                       "review_state", "review_model", "review_note"),
    "label_facts": ("q_norm_hash", "topic"),
    "golden_labels": ("q_norm_hash",),
}


def _validate_schema(conn: sqlite3.Connection) -> None:
    """迁移收尾自检：预期表/列缺一就抛错，不让半完成迁移进入正常使用。"""
    for table, cols in REQUIRED.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            raise RuntimeError(f"迁移后缺表：{table}")
        for col in cols:
            if not _column_exists(conn, table, col):
                raise RuntimeError(f"迁移后缺列：{table}.{col}")


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


@contextmanager
def session(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_by_hash(conn: sqlite3.Connection, doc_hash: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_documents WHERE doc_hash = ?", (doc_hash,)).fetchone()


def upsert_document(conn: sqlite3.Connection, row: dict, *, overwrite: bool = False) -> str:
    """按 doc_hash 插入一条原文档记录；默认冲突时 DO NOTHING。

    旧实现无脑覆盖全部字段：调用方若拿一条已存在的 hash 再登记，会把 classified /
    superseded 重置成 pending_classify、丢掉 first_seen_at。需要改记录必须显式
    overwrite=True（如 intake 的路径迁移走单独的 UPDATE，不用本函数）。
    返回 'inserted' / 'exists'。
    """
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    action = "DO NOTHING"
    if overwrite:
        action = ("DO UPDATE SET "
                  + ",".join(f"{c}=excluded.{c}" for c in cols if c != "doc_hash"))
    sql = (f"INSERT INTO source_documents ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(doc_hash) {action}")
    cur = conn.execute(sql, [row[c] for c in cols])
    return "inserted" if cur.rowcount else "exists"


def record_batch(conn: sqlite3.Connection, **kw) -> None:
    """写批次摘要：普通 INSERT，batch_id 冲突直接报错，不静默覆盖上一批统计。"""
    cols = list(kw.keys())
    conn.execute(
        f"INSERT INTO batches ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [kw[c] for c in cols],
    )


def count_by(conn: sqlite3.Connection, field: str) -> dict[str, int]:
    if field not in COUNT_FIELDS:
        raise ValueError(f"不支持的统计字段：{field}")
    rows = conn.execute(
        f"SELECT COALESCE({field},'<null>') AS k, COUNT(*) AS n FROM source_documents GROUP BY 1 ORDER BY n DESC"
    ).fetchall()
    return {r["k"]: r["n"] for r in rows}


def backup_db(db_path: Path | str, backup_dir: Path | str | None = None,
              keep: int = BACKUP_KEEP) -> Path:
    """用 SQLite backup API 给事实源做热备份（含 WAL 里已提交未 checkpoint 的数据）。

    kb.sqlite 存账本、label_facts、golden_labels 与 purge 依赖的元数据，不可重建；
    purge --apply / batch_label --merge / intake 写库前都应先调一次。
    `shutil.copy2` 只拷主文件，会漏 -wal，不能用于备份。
    """
    src_path = Path(db_path)
    if not src_path.exists():
        raise FileNotFoundError(f"数据库不存在：{src_path}")
    bdir = Path(backup_dir) if backup_dir else src_path.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"{src_path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
            out.execute("PRAGMA quick_check")
        finally:
            out.close()
    finally:
        src.close()
    for old in sorted(bdir.glob(f"{src_path.stem}-*.db"))[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dst
