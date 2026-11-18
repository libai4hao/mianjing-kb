# -*- coding: utf-8 -*-
"""SQLite 连接与迁移。表结构见 meta/schema/001~009（docs/03 §10.2）。

迁移口径（2026-11-19 统一，替换原先"SQL 文件 + db.py 里一串 _ensure_*"两套逻辑）：
- 唯一入口是 `MIGRATIONS` 有序注册表，每一项是 (版本号, 幂等函数)；
- 已应用的版本记在 `schema_version(version, applied_at)`；
- SQL 文件只是函数的载荷；006（questions 题级 topic）没有独立 SQL，就在代码里实现，
  版本号照记，不再出现"006 找不到迁移文件"的歧义。

事务与并发（T14，2026-11-19 二审）：
- 每个迁移单独一个 `BEGIN IMMEDIATE` 事务，迁移体 + 版本记账同生共死：中途失败回滚到
  "这个版本没发生过"，下次连接从该版本续跑，不会留下半张表/半列；
- 版本表在**锁内二次校验**：两个进程同时 migrate 时，后拿到写锁的进程会看到版本已落地，
  直接放行而不是重放 DDL；拿锁失败按 busy_timeout 排队，超时才报错；
- `executescript` 会隐式 COMMIT（把外面的事务顶掉），所以 SQL 文件一律用
  `split_sql()` 拆成单句 `execute`；脚本里的 `PRAGMA` 在事务内无效（journal_mode 还会
  直接报错），由 `exec_script(skip_pragma=True)` 跳过——连接层已经设过。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config, events, status

SCHEMA_FILE = Path(config.META_DIR) / "schema" / "001_init.sql"
NOW_FMT = "%Y-%m-%dT%H:%M:%S"
BACKUP_KEEP = 5
BEGIN_RETRIES = 5
COUNT_FIELDS = {"status", "category", "collection", "source_trust", "topic",
                "sub_topic", "ext", "parent_dir", "batch_id", "gate_result"}

# batches.state（T11）：intake 分块提交时先写 running，正常结束写 completed，异常写 failed
BATCH_RUNNING = "running"
BATCH_COMPLETED = "completed"
BATCH_FAILED = "failed"
BATCH_STATES = (BATCH_RUNNING, BATCH_COMPLETED, BATCH_FAILED)
_BATCH_COUNT_FIELDS = ("added", "duplicated", "moved", "quarantined", "ignored")


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
    # busy_timeout 必须先设：journal_mode 切换本身要拿写锁，并发打开时没有它就直接报 locked
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")   # kb.sqlite 是事实源，断电不丢已提交事务
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """按 schema_version 逐个应用未落地的迁移（每项都幂等）。"""
    _apply_migrations(conn)


def split_sql(sql: str) -> list[str]:
    """把 SQL 脚本拆成单句：跳过 `--`/`/* */` 注释，引号内的 `;` 不算分隔符。

    为什么要拆：`executescript` 在执行前会隐式 COMMIT，迁移的事务性会被它顶掉；
    改成逐句 `execute` 才能让"迁移体 + 版本记账"待在同一个事务里。
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:      # SQL 里 '' / "" 是转义
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            buf.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            buf.append(" ")
            continue
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def exec_script(conn: sqlite3.Connection, sql: str, *, skip_pragma: bool = True) -> int:
    """逐句执行脚本（事务安全版 executescript）。返回实际执行的语句数。"""
    n = 0
    for stmt in split_sql(sql):
        if skip_pragma and stmt.lstrip().upper().startswith("PRAGMA"):
            continue
        conn.execute(stmt)
        n += 1
    return n


def _read_schema(name: str) -> str:
    return (SCHEMA_FILE.parent / name).read_text(encoding="utf-8")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _ensure_parent_dir(conn: sqlite3.Connection) -> None:
    """补 parent_dir（父目录名）字段 + 可入库清单视图（002）。"""
    if not _column_exists(conn, "source_documents", "parent_dir"):
        conn.execute("ALTER TABLE source_documents ADD COLUMN parent_dir TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sd_parent ON source_documents(parent_dir)")
    # 注意：本迁移已上线，视图定义不得再改；口径要演进必须新增迁移。
    conn.execute("DROP VIEW IF EXISTS ingest_queue")
    conn.execute(
        f"""
        CREATE VIEW ingest_queue AS
        SELECT doc_hash, file_name, parent_dir, dir_path, ext, size_bytes,
               category, topic, sub_topic, collection, source_trust
        FROM source_documents
        WHERE status = '{status.CLASSIFIED}'
          AND dir_path NOT LIKE '{config.QUARANTINE_PREFIX}%'
        """
    )


def _ensure_source_spans(conn: sqlite3.Connection) -> None:
    """补合并版"源边界"字段（003）：source_count / sources_json（见 meta/schema/003）。"""
    for col, decl in (("source_count", "INTEGER"), ("sources_json", "TEXT")):
        if not _column_exists(conn, "source_documents", col):
            conn.execute(f"ALTER TABLE source_documents ADD COLUMN {col} {decl}")


def _ensure_questions(conn: sqlite3.Connection) -> None:
    """补 questions 表 + 频次视图（004，P1-1 extract 落表）。"""
    exec_script(conn, _read_schema("004_questions.sql"))


def _ensure_labels_staging(conn: sqlite3.Connection) -> None:
    """补 labels_staging 打标暂存表（005，docs/15 §3.6——G6 通过才 merge）。"""
    exec_script(conn, _read_schema("005_labels_staging.sql"))


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
    exec_script(conn, _read_schema("007_label_facts.sql"))
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
    exec_script(conn, _read_schema("008_golden_labels.sql"))


def _ensure_review_columns(conn: sqlite3.Connection) -> None:
    """009：labels_staging 加复核三列（批次三 R1，2026-11-19）。

    逐列判存在：旧实现整脚本跳过一次后，中途失败会留下"只加了一列"的库，
    下次启动看到 review_state 就跳过，另外两列永久缺失。
    """
    for col in ("review_state", "review_model", "review_note"):
        if not _column_exists(conn, "labels_staging", col):
            conn.execute(f"ALTER TABLE labels_staging ADD COLUMN {col} TEXT")


def _ensure_batch_state(conn: sqlite3.Connection) -> None:
    """010：batches 加 state / updated_at（T11 分块提交的批次进度）。

    老行默认 completed（都是已经跑完的历史批次）；state 取值见 BATCH_STATES。
    """
    if not _column_exists(conn, "batches", "state"):
        conn.execute(f"ALTER TABLE batches ADD COLUMN state TEXT NOT NULL "
                     f"DEFAULT '{BATCH_COMPLETED}'")
    if not _column_exists(conn, "batches", "updated_at"):
        conn.execute("ALTER TABLE batches ADD COLUMN updated_at TEXT")


def _ensure_document_events(conn: sqlite3.Connection) -> None:
    """011：document_events 事件表（T12）+ 历史 note 回填。

    回填放在迁移里，是为了让"旧库第一次被新代码打开"就完成 note → 事件的一次性搬迁；
    事件写入是 INSERT OR IGNORE，重复跑不会重复记。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_events (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_hash  TEXT NOT NULL,
          kind      TEXT NOT NULL,
          payload   TEXT NOT NULL DEFAULT '{}',
          at        TEXT NOT NULL,
          UNIQUE (doc_hash, kind, payload)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_de_doc  ON document_events(doc_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_de_kind ON document_events(kind)")
    events.backfill(conn)


def _ensure_label_dimensions(conn: sqlite3.Connection) -> None:
    """012（T21）：标签维度扩展——questions / labels_staging 加 position/quality/canonical_q。

    - position / quality 是多选，落库为 JSON 数组字符串；
    - canonical_q 是"标准化问句"（≤60 字），供检索与去重，不同于 q_norm（去空白归一）；
    - labels_staging 另加 tag_norm_hits：本次打标被同义词归一的标签数，用于量化合并率。
    逐列判存在：迁移中断后重跑只补缺列（与 009 同口径）。
    """
    for col, decl in (("position", "TEXT"), ("quality", "TEXT"), ("canonical_q", "TEXT")):
        if not _column_exists(conn, "questions", col):
            conn.execute(f"ALTER TABLE questions ADD COLUMN {col} {decl}")
        if not _column_exists(conn, "labels_staging", col):
            conn.execute(f"ALTER TABLE labels_staging ADD COLUMN {col} {decl}")
    if not _column_exists(conn, "labels_staging", "tag_norm_hits"):
        conn.execute("ALTER TABLE labels_staging ADD COLUMN tag_norm_hits INTEGER")


def _ensure_label_taxonomy(conn: sqlite3.Connection) -> None:
    """013（T24）：标签配置表 label_taxonomy + 从 meta/tag_vocab.yaml 灌一二级预设。

    配置表是运行时事实源（增删/停用走 `python -m mianjing_kb.taxonomy`）；
    seed 只灌空表，已存在的 (level1, level2) 不覆盖，避免升级时冲掉人工调整。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS label_taxonomy (
          id       INTEGER PRIMARY KEY AUTOINCREMENT,
          level1   TEXT NOT NULL,
          level2   TEXT NOT NULL,
          synonyms TEXT NOT NULL DEFAULT '[]',
          enabled  INTEGER NOT NULL DEFAULT 1,
          sort     INTEGER NOT NULL DEFAULT 0,
          UNIQUE (level1, level2)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tax_l1 ON label_taxonomy(level1, sort)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tax_l2 ON label_taxonomy(level2)")
    from . import taxonomy
    taxonomy.seed(conn)


def _init_schema(conn: sqlite3.Connection) -> None:
    """001：基础表（source_documents / batches 等）。脚本本身 IF NOT EXISTS，可直接重放。"""
    exec_script(conn, SCHEMA_FILE.read_text(encoding="utf-8"))


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
    ("010_batches_state", _ensure_batch_state),
    ("011_document_events", _ensure_document_events),
    ("012_label_dimensions", _ensure_label_dimensions),
    ("013_label_taxonomy", _ensure_label_taxonomy),
]


def _version_applied(conn: sqlite3.Connection, version: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM schema_version WHERE version=? LIMIT 1", (version,)
    ).fetchone() is not None


def _begin_immediate(conn: sqlite3.Connection, retries: int = BEGIN_RETRIES) -> None:
    """拿写锁开事务；并发进程在 busy_timeout 内排队，抢不到才退避重试。"""
    if conn.in_transaction:
        conn.commit()
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as ex:
            if "locked" not in str(ex).lower() or attempt == retries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _apply_one(conn: sqlite3.Connection, version: str, fn) -> str:
    """应用单个迁移：事务内二次校验版本 → 执行 → 记账 → 提交。返回 applied/already。"""
    _begin_immediate(conn)
    try:
        if _version_applied(conn, version):      # 锁内二次校验：别的进程可能刚跑完
            conn.commit()
            return "already"
        fn(conn)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                     (version, datetime.now().strftime(NOW_FMT)))
        conn.commit()
        return "applied"
    except BaseException:
        conn.rollback()
        raise


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    for version, fn in MIGRATIONS:
        _apply_one(conn, version, fn)
    _validate_schema(conn)


REQUIRED = {
    "source_documents": ("doc_hash", "sha256", "src_path", "dir_path", "status",
                         "parent_dir", "source_count", "sources_json"),
    "questions": ("qid", "doc_hash", "topic", "difficulty", "tags",
                  "position", "quality", "canonical_q"),
    "labels_staging": ("q_norm_hash", "prompt_tokens", "completion_tokens",
                       "review_state", "review_model", "review_note",
                       "position", "quality", "canonical_q", "tag_norm_hits"),
    "label_facts": ("q_norm_hash", "topic"),
    "golden_labels": ("q_norm_hash",),
    "batches": ("batch_id", "state", "updated_at"),
    "document_events": ("doc_hash", "kind", "payload", "at"),
    "label_taxonomy": ("level1", "level2", "synonyms", "enabled", "sort"),
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


def start_batch(conn: sqlite3.Connection, *, batch_id: str, root: str, mode: str,
                at: str | None = None) -> None:
    """批次开始：先落一行 state=running，后面靠分块提交更新进度（T11）。

    先落 running 的好处：中途崩溃时，库里有这批的"存在证明"与已提交条数，
    下次重扫能看出上一批是失败了还是还在跑。
    """
    conn.execute(
        "INSERT INTO batches (batch_id, created_at, root, mode, state, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (batch_id, at or datetime.now().strftime(NOW_FMT), root, mode,
         BATCH_RUNNING, at or datetime.now().strftime(NOW_FMT)),
    )


def update_batch(conn: sqlite3.Connection, batch_id: str, *, state: str | None = None,
                 note: str | None = None, **counts) -> None:
    """更新批次状态/计数（白名单字段，防止写错列名）。"""
    sets, args = [], []
    if state is not None:
        if state not in BATCH_STATES:
            raise ValueError(f"未知批次状态：{state}")
        sets.append("state=?")
        args.append(state)
    if note is not None:
        sets.append("note=?")
        args.append(note)
    for k, v in counts.items():
        if k not in _BATCH_COUNT_FIELDS:
            raise ValueError(f"不支持的批次计数字段：{k}")
        sets.append(f"{k}=?")
        args.append(v)
    sets.append("updated_at=?")
    args.append(datetime.now().strftime(NOW_FMT))
    args.append(batch_id)
    conn.execute(f"UPDATE batches SET {','.join(sets)} WHERE batch_id=?", args)


def batch_state(conn: sqlite3.Connection, batch_id: str) -> str | None:
    row = conn.execute("SELECT state FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    return row["state"] if row else None


def count_by(conn: sqlite3.Connection, field: str) -> dict[str, int]:
    if field not in COUNT_FIELDS:
        raise ValueError(f"不支持的统计字段：{field}")
    rows = conn.execute(
        f"SELECT COALESCE({field},'<null>') AS k, COUNT(*) AS n "
        "FROM source_documents GROUP BY 1 ORDER BY n DESC"
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
