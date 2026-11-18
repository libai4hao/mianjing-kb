# -*- coding: utf-8 -*-
"""refresh：检索库影子重建 + G1-G5 门禁 + 原子切换（docs/15 §2.1 / K1，2026-11-17 落地）。

流程：
  1. 运行锁：build/refresh.lock（O_CREAT|O_EXCL 写 PID）；启动清理发现残留文件即中止
  2. 影子重建：build_db.build(out_db=BUILD/面经库_shadow.db)
  3. 向量：默认从现行库按 rowid 拷（先做 q_norm_hash 对齐 + 孤儿向量 + 两侧空哈希校验）；
     --reembed 时不拷，直接重嵌入，并要求向量数 == 题目数
  4. 门禁：G1-G5 全部实跑；硬门只认 PASS，未知状态直接判失败
  5. 完整性：切换前 quick_check 影子；切换后再 quick_check + 行数比对，不一致自动从快照回滚
  6. 快照：SQLite backup API（ro 连接），能拿到 WAL 里已提交未 checkpoint 的数据；
     copy2 只拷主文件，会做出一份过时备份
  7. 切换：os.replace 同卷原子；成功后再清旧库侧车（失败只告警，不再谎报"线上未动"）
  8. 收尾：只有切换成功才清理旧快照（按文件名时间戳排序，不用 mtime）

用法：
    python tools/refresh.py               # 影子重建 → 向量拷贝 → G1-G5 → 通过才切换
    python tools/refresh.py --reembed     # 影子重建后重跑向量化（新题入库后必用）
    python tools/refresh.py --swap-only   # 只重试切换（os.replace 被占用失败后用）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, wlock  # noqa: E402

SHADOW_NAME = "面经库_shadow.db"
ONLINE_NAME = "面经库.db"
ROLLBACK_DIR = "rollback"      # 切换前把线上库留一份，给回滚窗口
ROLLBACK_KEEP = 3              # 只留最近 3 份，避免把盘塞满
LOCK_NAME = "refresh.lock"
KB_LOCK_WAIT_SEC = 10          # 等别的写库工具让路的上限；超时就放弃，不硬抢
LOG_NAME = "refresh_log.jsonl"
REQUIRED_GATES = ("G1", "G2", "G3", "G4", "G5")
KNOWN_STATES = ("PASS", "WARN", "FAIL", "SKIP")


def _sidecars(p: Path) -> tuple[Path, Path]:
    return Path(str(p) + "-wal"), Path(str(p) + "-shm")


def _ro_uri(p: Path) -> str:
    return f"file:{p.as_posix()}?mode=ro"


def _quick_check(p: Path) -> str:
    """PRAGMA quick_check（只读打开，不改库）。返回 'ok' 或问题描述。"""
    con = sqlite3.connect(_ro_uri(p), uri=True)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


def _counts(p: Path) -> dict:
    """行数快照：questions / doc_contents / vec_questions（无向量表记 None）。"""
    con = sqlite3.connect(_ro_uri(p), uri=True)
    try:
        out = {}
        for t in ("questions", "doc_contents"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
        try:
            out["vec_questions"] = con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        except sqlite3.OperationalError:
            out["vec_questions"] = None
        return out
    finally:
        con.close()


def _acquire_lock(build_dir: Path) -> Path:
    """排他锁：并发 refresh / refresh 撞 merge 时，第二个进程直接失败。"""
    p = build_dir / LOCK_NAME
    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)
    return p


def _release_lock(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _cleanup(shadow: Path) -> list[str]:
    """删影子及其侧车；返回删不掉的项（调用方决定是否中止）。"""
    notes = []
    for p in (shadow, *_sidecars(shadow)):
        try:
            p.unlink(missing_ok=True)
        except OSError as ex:
            notes.append(f"{p.name}: {type(ex).__name__}")
    return notes


def _snapshot(online: Path) -> Path | None:
    """切换前快照线上库：SQLite backup API（ro 连接），WAL 里已提交数据也会带上。

    copy2 只复制主文件，-wal 里的数据会丢，做出来的"回滚备份"是不可靠的。
    """
    if not online.exists():
        return None
    d = online.parent / ROLLBACK_DIR
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{online.stem}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(_ro_uri(online), uri=True)
    dst_con = sqlite3.connect(str(dst))
    try:
        src.backup(dst_con)
        dst_con.commit()
    finally:
        dst_con.close()
        src.close()
    return dst


def _snapshot_gc(online: Path, keep: int = ROLLBACK_KEEP) -> list[str]:
    """只保留最近 keep 份快照；按文件名里的时间戳排序（mtime 会被复制继承，不可靠）。

    只在切换成功之后调用——切换失败时旧快照是唯一的回滚依据，不能删。
    """
    d = online.parent / ROLLBACK_DIR
    if not d.exists():
        return []
    files = sorted(d.glob(f"{online.stem}-*.db"), key=lambda p: p.name, reverse=True)
    removed = []
    for p in files[keep:]:
        try:
            p.unlink(missing_ok=True)
            removed.append(p.name)
        except OSError:
            pass
    return removed


def _remove_sidecars(online: Path) -> list[str]:
    """清理被废弃旧库的 -wal/-shm；失败只记警告（非致命，不能谎报切换失败）。"""
    warns = []
    for p in _sidecars(online):
        try:
            p.unlink(missing_ok=True)
        except OSError as ex:
            warns.append(f"{p.name}: {type(ex).__name__}: {ex}")
    return warns


def _remove_shadow_sidecars(shadow: Path) -> list[str]:
    """只清影子的侧车（--swap-only 保留影子的主文件）。"""
    notes = []
    for p in _sidecars(shadow):
        try:
            p.unlink(missing_ok=True)
        except OSError as ex:
            notes.append(f"{p.name}: {type(ex).__name__}")
    return notes


def _restore(snapshot: Path, online: Path) -> str:
    """用快照覆盖线上库（快照本身保留，供人工回滚）。"""
    _remove_sidecars(online)
    shutil.copy2(snapshot, online)
    return f"已从快照 {snapshot.name} 回滚线上库"


def build_shadow(shadow: Path) -> dict:
    from build_db import build
    return build(out_db=shadow)


def copy_vec(src_db: Path, dst_db: Path) -> dict:
    """把现行库 vec_questions 回填进影子库；返回结构化结果。

    返回 {ok, kind, detail?, vectors, src_vectors, questions}；kind ∈
    absent / copied / misaligned / null_hash / orphan_vectors / no_sqlite_vec / vec_short / error。
    """
    con = sqlite3.connect(f"file:{dst_db.as_posix()}?mode=rwc", uri=True)
    try:
        con.execute("ATTACH DATABASE ? AS cur", (_ro_uri(src_db),))
        has = con.execute("SELECT COUNT(*) FROM cur.sqlite_master "
                          "WHERE type='table' AND name='vec_questions'").fetchone()[0]
        if not has:
            return {"ok": True, "kind": "absent", "detail": "现行库无向量表"}
        n_q_dst = con.execute("SELECT COUNT(*) FROM main.questions").fetchone()[0]
        # ① 两侧 q_norm_hash 同为空时，下面的对齐校验会"全等于空"而误判通过
        both_empty = con.execute(
            "SELECT COUNT(*) FROM cur.questions c JOIN main.questions m ON m.qid=c.qid "
            "WHERE COALESCE(c.q_norm_hash,'')='' AND COALESCE(m.q_norm_hash,'')=''"
        ).fetchone()[0]
        if both_empty:
            return {"ok": False, "kind": "null_hash",
                    "detail": f"{both_empty} 行两侧 q_norm_hash 均为空，无法校验向量归属"}
        try:
            mis = con.execute(
                "SELECT COUNT(*) FROM cur.questions c LEFT JOIN main.questions m "
                "ON m.qid=c.qid "
                "WHERE m.qid IS NULL OR COALESCE(c.q_norm_hash,'')!=COALESCE(m.q_norm_hash,'')"
            ).fetchone()[0]
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "kind": "error",
                    "detail": f"向量对齐校验失败（questions 表/列缺失？）{type(ex).__name__}: {str(ex)[:80]}"}
        if mis:
            return {"ok": False, "kind": "misaligned",
                    "detail": f"现行库 {mis} 行 qid/q_norm_hash 与影子库不匹配——重切后请 --reembed"}
        # ② 现行库孤立的向量行（rowid 找不到题目）不能拷
        orphan = con.execute(
            "SELECT COUNT(*) FROM cur.vec_questions v LEFT JOIN cur.questions q ON q.qid=v.rowid "
            "WHERE q.qid IS NULL").fetchone()[0]
        if orphan:
            return {"ok": False, "kind": "orphan_vectors",
                    "detail": f"现行库有 {orphan} 条孤立向量（rowid 无对应题目）"}
        try:
            import sqlite_vec
        except ImportError:
            return {"ok": False, "kind": "no_sqlite_vec", "detail": "sqlite_vec 不可用"}
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        from embed_questions import DIM
        con.execute(f"CREATE VIRTUAL TABLE vec_questions USING vec0(embedding float[{DIM}])")
        con.execute("INSERT INTO vec_questions(rowid, embedding) "
                    "SELECT rowid, embedding FROM cur.vec_questions")
        n_dst = con.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        n_src = con.execute("SELECT COUNT(*) FROM cur.vec_questions").fetchone()[0]
        con.commit()
        if n_dst != n_src:
            return {"ok": False, "kind": "error", "detail": f"向量回填行数不一致 {n_dst}!={n_src}"}
        if n_dst < n_q_dst:
            return {"ok": False, "kind": "vec_short",
                    "detail": f"向量 {n_dst} < 题目 {n_q_dst}——请用 --reembed 重建向量"}
        return {"ok": True, "kind": "copied", "vectors": n_dst,
                "src_vectors": n_src, "questions": n_q_dst}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "kind": "error", "detail": f"{type(ex).__name__}: {str(ex)[:120]}"}
    finally:
        con.close()


def check_reembed(shadow: Path, emb: dict) -> str | None:
    """--reembed 后校验：向量数必须等于题目数，否则不许切换。"""
    q = _counts(shadow)
    n_vec, n_q = q.get("vec_questions"), q.get("questions")
    if emb.get("vectors") is None or n_vec is None:
        return "重嵌入未产出向量表"
    if n_vec != n_q:
        return f"重嵌入向量 {n_vec} != 题目 {n_q}"
    return None


def run_gates(shadow: Path) -> list[dict]:
    """G1-G5 对影子库实跑（G6 打标、G7 文档一致性不在此跑）。"""
    from verify_ledger import (g1_coverage, g2_chain, g3_topic, g4_index, g5_disk,
                               load_skip_log)
    kb_conn = sqlite3.connect(_ro_uri(Path(config.DB_PATH)), uri=True)
    kb_conn.row_factory = sqlite3.Row
    out_conn = sqlite3.connect(_ro_uri(shadow), uri=True)
    out_conn.row_factory = sqlite3.Row
    try:
        import sqlite_vec
        out_conn.enable_load_extension(True)
        sqlite_vec.load(out_conn)
        out_conn.enable_load_extension(False)
    except ImportError:
        pass
    skip = load_skip_log()
    gates = [
        g1_coverage(kb_conn, out_conn, skip),
        g2_chain(out_conn, Path(config.QUESTION_MD_DIR), Path(config.BUILD_DIR) / "ocr_text", skip),
        g3_topic(out_conn),
        g4_index(out_conn),
        g5_disk(kb_conn, Path(config.CORPUS_ROOT), Path(config.BUILD_DIR)),
    ]
    kb_conn.close()
    out_conn.close()
    return gates


def check_gates(gates: list[dict]) -> list[str]:
    """白名单校验：G1-G5 必须齐全；未知状态直接失败；硬门的 FAIL/SKIP 阻止切换。

    硬门 WARN 不阻止（与 G6b 小样本 WARN 同一口径：只提示，不替人拍板）；
    SKIP 说明关卡根本没跑，按失败处理，不能当成通过。
    """
    problems = []
    by_id = {g.get("id"): g for g in gates}
    for gid in REQUIRED_GATES:
        g = by_id.get(gid)
        if g is None:
            problems.append(f"{gid} 缺失")
            continue
        state = g.get("state")
        if state not in KNOWN_STATES:
            problems.append(f"{gid} 未知状态 {state!r}")
            continue
        if g.get("level") == "硬" and state in ("FAIL", "SKIP"):
            problems.append(f"{gid} 硬门 {state}")
    return problems


def _disk_guard(online: Path, build_dir: Path) -> str | None:
    """磁盘余量：重建影子 + 快照 ≈ 线上库两份，再留 100MB 余量。"""
    if not online.exists():
        return None
    need = online.stat().st_size * 2 + 100 * 1024 * 1024
    free = shutil.disk_usage(build_dir).free
    if free < need:
        return (f"磁盘余量不足：需要约 {need / 1024 / 1024:.0f} MB，"
                f"可用 {free / 1024 / 1024:.0f} MB")
    return None


def _log(build_dir: Path, summary: dict) -> None:
    try:
        with (build_dir / LOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                                 **summary}, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检索库影子重建 + G1-G5 门禁 + 原子切换")
    ap.add_argument("--reembed", action="store_true",
                    help="影子重建后重跑向量化（新题入库后必用；默认只拷现行库向量）")
    ap.add_argument("--swap-only", action="store_true",
                    help="跳过重建/门禁，只对已建好的影子库重试切换")
    args = ap.parse_args(argv)

    started = time.perf_counter()
    build_dir = Path(config.BUILD_DIR)
    build_dir.mkdir(parents=True, exist_ok=True)
    shadow = build_dir / SHADOW_NAME
    online = build_dir / ONLINE_NAME

    try:
        lock = _acquire_lock(build_dir)
    except FileExistsError:
        holder = ""
        try:
            holder = (build_dir / LOCK_NAME).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        print(json.dumps({"verdict": "ABORT",
                          "why": f"已有 refresh 在跑（lock pid={holder or '?'}）；"
                                 "若是异常退出残留，确认后删除 refresh.lock 重试"},
                         ensure_ascii=False, indent=1))
        return 2

    # T15：refresh 也是写库工具，与 intake / batch_label / doc_convert 共用同一把写锁，
    # 否则 refresh 重建影子库期间别人往线上库写字，切换后这些写入会凭空消失。
    kb_lock = wlock.write_lock("kb", tool="refresh", build_dir=build_dir,
                               wait_sec=KB_LOCK_WAIT_SEC)
    try:
        kb_lock.__enter__()
    except wlock.LockHeld as ex:
        _release_lock(lock)
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "已有写库工具在写 kb/线上库，refresh 不与之并行；稍后重试"},
                         ensure_ascii=False, indent=1))
        return 2

    swapped = False
    summary: dict = {}
    vec: dict | str = "skip"
    try:
        if args.swap_only:
            if not shadow.exists():
                print(json.dumps({"verdict": "ABORT", "why": f"影子库不存在：{shadow}"},
                                 ensure_ascii=False, indent=1))
                return 2
            stale = _remove_shadow_sidecars(shadow)
            if stale:
                print(json.dumps({"verdict": "ABORT",
                                  "why": "影子库侧车删不掉，拒绝在旧 WAL 上切换",
                                  "leftover_locked": stale}, ensure_ascii=False, indent=1))
                return 2
        else:
            notes = _cleanup(shadow)
            if notes:
                print(json.dumps({"verdict": "ABORT",
                                  "why": "上次残留的影子文件删不掉，拒绝在旧文件上继续",
                                  "leftover_locked": notes}, ensure_ascii=False, indent=1))
                return 2

        if args.swap_only:
            stats = None
        else:
            why = _disk_guard(online, build_dir)
            if why:
                print(json.dumps({"verdict": "ABORT", "why": why}, ensure_ascii=False, indent=1))
                return 2
            stats = build_shadow(shadow)
            if args.reembed:
                from embed_questions import run as embed_run
                emb = embed_run(out_db=shadow)
                err = check_reembed(shadow, emb)
                if err:
                    _cleanup(shadow)
                    print(json.dumps({"verdict": "ABORT", "why": f"重嵌入校验失败：{err}",
                                      "note": "线上 面经库.db 未动"}, ensure_ascii=False, indent=1))
                    return 1
                vec = {"kind": "reembed", "vectors": emb.get("vectors")}
            else:
                res = copy_vec(online, shadow) if online.exists() else {
                    "ok": True, "kind": "absent", "detail": "线上库不存在"}
                if not res.get("ok"):
                    _cleanup(shadow)
                    print(json.dumps({"verdict": "ABORT",
                                      "why": f"向量回填失败: {res.get('kind')}: {res.get('detail', '')}",
                                      "note": "线上 面经库.db 未动；可 --reembed 重跑向量化后重试"},
                                     ensure_ascii=False, indent=1))
                    return 1
                vec = {k: v for k, v in res.items() if k != "ok"}

            gates = run_gates(shadow)
            problems = check_gates(gates)
            summary = {
                "verdict": "PASS" if not problems else "FAIL",
                "build": {k: stats[k] for k in ("documents", "questions", "doc_contents", "size_mb")},
                "vec": vec,
                "gates": {g["id"]: g["state"] for g in gates},
                "gate_problems": problems,
            }
            if problems:
                _cleanup(shadow)
                summary["note"] = "影子库已删除，线上 面经库.db 未动（门禁阻止力，docs/15 §2.1）"
                summary["elapsed_sec"] = round(time.perf_counter() - started, 1)
                _log(build_dir, summary)
                print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
                return 1

        # 切换前完整性：影子必须能过 quick_check
        qc_shadow = _quick_check(shadow)
        if qc_shadow != "ok":
            _cleanup(shadow)
            print(json.dumps({"verdict": "ABORT", "why": f"影子库 quick_check 失败：{qc_shadow}",
                              "note": "线上 面经库.db 未动"}, ensure_ascii=False, indent=1))
            return 1
        pre = _counts(shadow)

        snapshot = _snapshot(online)
        try:
            os.replace(shadow, online)
        except OSError as ex:
            print(json.dumps({"verdict": "ABORT", "why": f"切换失败：{type(ex).__name__}: {ex}",
                              "note": f"影子库保留在 {shadow.name}，可用 --swap-only 重试；"
                                      "线上 面经库.db 未动"},
                             ensure_ascii=False, indent=1))
            return 1
        swapped = True

        warns = _remove_sidecars(online)
        post = _counts(online)
        qc_online = _quick_check(online)
        bad = []
        if qc_online != "ok":
            bad.append(f"线上 quick_check 失败：{qc_online}")
        if post != pre:
            bad.append(f"切换后行数不一致：{post} != {pre}")
        summary = summary or {"verdict": "PASS", "mode": "--swap-only"}
        summary.update({
            "vec": summary.get("vec", vec),
            "swapped": True,
            "post_check": {"quick_check": qc_online, "counts": post},
            "warnings": warns,
            "snapshot": snapshot.name if snapshot else None,
        })
        if bad:
            rollback = _restore(snapshot, online) if snapshot else "无快照可回滚"
            summary.update({"verdict": "ROLLED_BACK", "why": bad, "rollback": rollback})
            summary["elapsed_sec"] = round(time.perf_counter() - started, 1)
            _log(build_dir, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
            return 1

        removed = _snapshot_gc(online)
        summary.update({
            "verdict": "PASS",
            "note": ("影子库已原子切换为线上 面经库.db"
                     + (f"；旧库快照 {snapshot.name}（{ROLLBACK_DIR}/，回滚用）" if snapshot else "")),
            "snapshot_gc": removed,
            "elapsed_sec": round(time.perf_counter() - started, 1),
        })
        _log(build_dir, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        return 0
    except Exception as ex:  # noqa: BLE001 —— 刷新失败必须保住线上库
        leftover = _cleanup(shadow) if not swapped else []
        keep = shadow.exists() and not swapped
        print(json.dumps({"verdict": "ABORT" if not swapped else "ERROR_AFTER_SWAP",
                          "why": f"{type(ex).__name__}: {str(ex)[:200]}",
                          "leftover_locked": leftover,
                          "shadow_kept": keep,
                          "note": ("线上 面经库.db 未动" if not swapped
                                   else "切换已发生，请核对线上库与侧车")},
                         ensure_ascii=False, indent=1))
        return 1
    finally:
        kb_lock.__exit__(None, None, None)
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
