# -*- coding: utf-8 -*-
"""代码 ↔ 文档一致性核查（2026-11-19，Q-2/Q-3 之后的例行检查）。

只查"机器可判定"的漂移，不做语义评审：
  1. `kb/status.py` 的 ALL vs `meta/schema/001_init.sql` 的 CHECK 枚举；
  2. `docs/19` 的状态机清单 vs `status.ALL`；
  3. `docs/03` §10.4 表里出现的状态是否都在 `status.ALL`（子集即可）；
  4. `docs/15` 门禁表 vs `verify_ledger` 实际 `_gate("G#", ...)`，
     且 `refresh.REQUIRED_GATES` 必须全部在文档表里；
  5. README 是否收录了每一份 `docs/*.md`，README 引用的文件是否存在；
  6. 每份现行 docs 是否带状态头（`> 状态：`）。

用法：
    python -m mianjing_kb.doc_audit            # 有问题 exit 1
    python -m mianjing_kb.doc_audit --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CHECK_RE = re.compile(r"CHECK\s*\(\s*status\s+IN\s*\((.*?)\)\s*\)", re.S)
BACKTICK_RE = re.compile(r"`([^`]+)`")
GATE_DOC_RE = re.compile(r"\|\s*\*\*(G[0-9A-Za-z]+)\*\*\s*\|\s*([^|]+?)\s*\|")
GATE_CODE_RE = re.compile(r'_gate\(\s*"(G[0-9A-Za-z]+)"\s*,\s*"([^"]+)"')
DOC_REF_RE = re.compile(r"`((?:\d+|archive/|项目审核报告/)[^`]*?\.md)`")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def status_schema(conn_text: str) -> set[str]:
    m = CHECK_RE.search(conn_text)
    if not m:
        return set()
    return set(re.findall(r"'([^']+)'", m.group(1)))


def docs19_states(text: str) -> set[str]:
    m = re.search(r"态清单：`(.+?)`", text, re.S)
    if not m:
        return set()
    return {s.strip() for s in m.group(1).split("/") if s.strip()}


def docs03_states(text: str) -> set[str]:
    """§10.4 状态表的取值：表格单元格里的代码状态与分组行里的反引号状态。"""
    sec = text.split("### 10.4", 1)
    if len(sec) < 2:
        return set()
    body = sec[1].split("### 10.5", 1)[0]
    states = set()
    for line in body.splitlines():
        if line.lstrip().startswith("|"):
            for cell in BACKTICK_RE.findall(line):
                for token in re.split(r"\s*/\s*", cell):
                    token = token.strip()
                    if re.fullmatch(r"[a-z][a-z_]+", token):
                        states.add(token)
    return states


def docs15_gates(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in GATE_DOC_RE.finditer(text)}


def code_gates(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in GATE_CODE_RE.finditer(text)}


def check_repo(repo: Path = REPO) -> dict:
    from mianjing_kb.kb import status

    issues: list[dict] = []
    meta_dir = repo / "meta" / "schema"
    docs = repo / "docs"

    # 1) status 枚举
    schema_states = status_schema(_read(meta_dir / "001_init.sql"))
    if not schema_states:
        issues.append({"kind": "cannot_parse", "what": "001_init.sql CHECK 枚举"})
    elif schema_states != set(status.ALL):
        issues.append({"kind": "status_enum_mismatch", "code": sorted(status.ALL),
                       "schema": sorted(schema_states)})

    # 2) docs/19
    d19 = docs19_states(_read(docs / "19-功能流程图.md"))
    if not d19:
        issues.append({"kind": "cannot_parse", "what": "docs/19 状态机清单"})
    elif d19 != set(status.ALL):
        issues.append({"kind": "status_docs19_mismatch",
                       "missing_in_doc": sorted(set(status.ALL) - d19),
                       "extra_in_doc": sorted(d19 - set(status.ALL))})

    # 3) docs/03 §10.4 子集
    d03 = docs03_states(_read(docs / "03-未分类文档入库方案.md"))
    if not d03:
        issues.append({"kind": "cannot_parse", "what": "docs/03 §10.4 状态表"})
    elif not d03 <= set(status.ALL):
        issues.append({"kind": "status_docs03_unknown", "unknown": sorted(d03 - set(status.ALL))})

    # 4) 门禁
    vl_text = _read(repo / "mianjing_kb" / "verify_ledger.py")
    g_code = code_gates(vl_text)
    g_doc = docs15_gates(_read(docs / "15-检验门禁与线上打标方案-2026-11-17.md"))
    if not g_code or not g_doc:
        issues.append({"kind": "cannot_parse", "what": "门禁表（docs/15 或 verify_ledger）"})
    else:
        for gid, name in g_code.items():
            if gid not in g_doc:
                issues.append({"kind": "gate_missing_in_docs15", "gate": gid})
            elif name not in g_doc[gid] and g_doc[gid] not in name:
                issues.append({"kind": "gate_name_mismatch", "gate": gid,
                               "code": name, "doc": g_doc[gid]})
    required = {"G1", "G2", "G3", "G4", "G5", "G8"}
    m = re.search(r"REQUIRED_GATES\s*=\s*\(([^)]*)\)", _read(repo / "mianjing_kb" / "refresh.py"))
    if m:
        required = {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}
    missing = sorted(required - set(g_doc))
    if missing:
        issues.append({"kind": "required_gates_missing_in_docs15", "gates": missing})

    # 5) README 文档清单（索引在 docs/README.md，不是仓库根 README）
    readme = _read(docs / "README.md")
    if not readme:
        issues.append({"kind": "cannot_parse", "what": "docs/README.md"})
    else:
        for p in sorted(docs.glob("*.md")):
            if p.name == "README.md":
                continue
            if p.name not in readme:
                issues.append({"kind": "doc_not_in_readme", "doc": p.name})
        for ref in DOC_REF_RE.findall(readme):
            if not (docs / ref).exists():
                issues.append({"kind": "readme_ref_missing", "ref": ref})

    # 6) 状态头（索引页 README 自身豁免）
    for p in sorted(docs.glob("*.md")):
        if p.name == "README.md":
            continue
        head = "\n".join(_read(p).splitlines()[:6])
        if not re.search(r"状态\*?\*?：", head):
            issues.append({"kind": "missing_status_header", "doc": p.name})

    return {"ok": not issues, "issues": issues,
            "counts": {"issues": len(issues), "docs": len(list(docs.glob("*.md"))) }}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="代码 ↔ 文档一致性核查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = check_repo()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        if res["ok"]:
            print(f"[ok] 文档一致性通过（{res['counts']['docs']} 份 docs）")
        else:
            print(f"[fail] {res['counts']['issues']} 处不一致：")
            for it in res["issues"]:
                print(" - " + json.dumps(it, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
