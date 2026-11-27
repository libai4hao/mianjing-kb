# -*- coding: utf-8 -*-
"""单文件 → md 转换器（T17：从语料侧 `21-面经数据库/tools/convert_one.py` 迁入版本管理）。

为什么迁：转换主逻辑原本只活在语料目录里，不受 git 管理——换台机器没配语料侧 tools 就
跑不了 reconvert，而且改动不可 review、不可回归。现在仓内版本是**主路**，语料侧同名
模块仅作 fallback（见 reconvert._load_convert_one）。

行为约定：函数签名与输出内容与语料侧原版保持一致（md 字节一致才有"哈希可回归"），
只做不改变输出的清理：删掉一段空转的 root.iter()、文件句柄用 with、CLI 的语料根改为
显式 `--corpus-root` / `KB_CORPUS_ROOT`（原版按 `__file__` 反推，挪进仓库后必然算错）。
唯一的有意输出偏差（2026-11-22 P2-3）：`pdf_to_md` 输出前做 `normalize_ascii_dot_space`
归并 PDF 文本层的英文标识符句点空格碎片（`Collections. unmodifiable` → `Collections.unmodifiable`），
会对转换产物做一版修复；语料侧原版无此步。

提取器链：pymupdf4llm（结构化）→ pypdf（纯文本）；质量门禁：CJK 比例。
用法（CLI，一般由 convert.py 批量调用）：
    python -m kb.convert_one <entry.json> <result.json> --corpus-root <语料根> [--extractor pypdf]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from mianjing_kb.kb import atomic, docx_safety, paths

VENDORED_FROM = "21-面经数据库/tools/convert_one.py"
VENDORED_AT = "2026-11-19"


def cjk_ratio(t: str) -> float:
    if not t:
        return 0.0
    return sum(1 for c in t if "\u4e00" <= c <= "\u9fff") / len(t)


_ASCII_DOT_SPACE = re.compile(r"([A-Za-z])\.\s+([A-Za-z])")


def normalize_ascii_dot_space(text: str) -> str:
    """把 PDF 提取时英文标识符 `X.y`（如 `Collections.unmodifiable`）被拆成
    `X. y` 的碎片归并回 `X.y`。只命中「ASCII 字母 + `.` + 空白 + ASCII 字母」，
    中文句号、数字列表、小数、无空格的完整标识符都不受影响。
    已知取舍：英文句号场景（如 `good. Next` 这类中英混排英文句）会被并入；
    中文技术文档里 `.` 几乎只出现在代码标识符/URL/版本号，误伤概率极低。
    """
    return _ASCII_DOT_SPACE.sub(r"\1.\2", text)


ILLEGAL = r'[\\/:*?"<>|\r\n\t]'


def safe_name(name: str) -> str:
    """把不可信目录/文件名收敛为单个普通路径段。"""
    value = re.sub(ILLEGAL, "_", str(name)).strip(" .")[:80]
    if value in ("", ".", ".."):
        return ""
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def img_hash(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()[:12]


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_md(path: str | Path) -> str:
    xml = docx_safety.read_docx_member(path, "word/document.xml")
    root = ET.fromstring(xml)
    lines: list[str] = []
    body = root.find(f"{W}body")

    def walk(el) -> None:
        for child in el:
            if child.tag == f"{W}p":
                style = ""
                ps = child.find(f"{W}pPr/{W}pStyle")
                if ps is not None:
                    style = ps.get(f"{W}val", "")
                text = "".join(t.text or "" for t in child.iter(f"{W}t")).strip()
                if not text:
                    continue
                m = re.match(r"^[Hh]eading(\d)", style) or re.match(r"^(\d)$", style)
                if m:
                    lines.append("#" * min(int(m.group(1)), 6) + " " + text)
                elif style.lower() == "title":
                    lines.append("# " + text)
                else:
                    lines.append(text)
                    lines.append("")
            elif child.tag == f"{W}tbl":
                rows = []
                for tr in child.findall(f"{W}tr"):
                    cells = []
                    for tc in tr.findall(f"{W}tc"):
                        cells.append("".join(t.text or "" for t in tc.iter(f"{W}t"))
                                     .strip().replace("|", "\\|"))
                    rows.append(cells)
                if rows:
                    ncol = max(len(r) for r in rows)
                    lines.append("| " + " | ".join(rows[0] + [""] * (ncol - len(rows[0]))) + " |")
                    lines.append("|" + " --- |" * ncol)
                    for r in rows[1:]:
                        lines.append("| " + " | ".join(r + [""] * (ncol - len(r))) + " |")
                    lines.append("")

    walk(body)
    return "\n".join(lines) + "\n"


def doc_to_text(path: str | Path) -> str:
    """老 .doc：Word COM 另存为纯文本。注意这里不处理超时（T16 单独排队）。"""
    import win32com.client
    # 使用独占临时文件，避免 tempfile.mktemp 的名称竞争。
    fd, out = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        # 必须在 Documents.Open 之前设置；3 = msoAutomationSecurityForceDisable。
        word.AutomationSecurity = 3
        d = word.Documents.Open(os.path.abspath(str(path)), ReadOnly=True,
                                AddToRecentFiles=False, UpdateLinks=0,
                                ConfirmConversions=False,
                                PasswordDocument="__no_password__",
                                WritePasswordDocument="__no_password__")
        try:
            d.SaveAs2(out, FileFormat=2)  # wdFormatText
        finally:
            d.Close(False)
        with open(out, "rb") as f:
            data = f.read()
    finally:
        try:
            if word is not None:
                word.Quit()
        finally:
            try:
                os.remove(out)
            except FileNotFoundError:
                pass
    for enc in ("gbk", "utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except Exception:                              # noqa: BLE001
            continue
    return data.decode("gbk", "ignore")


def txt_to_text(path: str | Path) -> str:
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8", "gbk", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except Exception:                              # noqa: BLE001
            continue
    return data.decode("gbk", "ignore")


def pdf_extract_images(doc, img_dir: str, doc_stem: str) -> tuple[list, int, int]:
    """提取图片，过滤小图和跨页装饰。返回 (refs, kept, skipped)。

    #65：页数/单图解码字节/图片张数/累计字节都有预算；超限记 skip 不崩溃，
    恶意超大图在解码前按 xref 尺寸拒绝。
    """
    import pymupdf

    from mianjing_kb.kb import pdfbudget
    os.makedirs(img_dir, exist_ok=True)
    pdfbudget.check_page_count(len(doc))
    page_counts: dict[str, set] = {}
    kept = []
    budget_skipped = 0
    total_bytes = 0
    for pno in range(len(doc)):
        for im in doc[pno].get_images(full=True):
            xref = im[0]
            if len(kept) >= pdfbudget.MAX_IMAGES_PER_DOC:
                budget_skipped += 1
                continue
            try:
                # im = (xref, smask, width, height, bpc, colorspace, ...)
                w, h = int(im[2]), int(im[3])
                pdfbudget.check_image_decode(w, h, 4)   # 按最坏 4 通道预估
                pix = pymupdf.Pixmap(doc, xref)
                if pix.width < 80 or pix.height < 80:
                    continue
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                pdfbudget.check_image_decode(pix.width, pix.height, pix.n)
                data = pix.tobytes("png")
            except pdfbudget.ResourceBudgetError:
                budget_skipped += 1
                continue
            except Exception:                          # noqa: BLE001
                continue
            if total_bytes + len(data) > pdfbudget.MAX_IMAGE_TOTAL_BYTES:
                budget_skipped += 1
                continue
            total_bytes += len(data)
            h_hash = img_hash(data)
            page_counts.setdefault(h_hash, set()).add(pno)
            kept.append((h_hash, pno, data))
    decorative = {h for h, pcs in page_counts.items() if len(pcs) >= max(4, len(doc) // 3)}
    saved: dict[str, tuple[str, int]] = {}
    for h, pno, data in kept:
        if h in decorative or h in saved:
            continue
        fn = f"{doc_stem}_{h}.png"
        with open(os.path.join(img_dir, fn), "wb") as f:
            f.write(data)
        saved[h] = (fn, pno + 1)
    return list(saved.values()), len(saved), len(kept) - len(saved) + budget_skipped


def pdf_to_md(path: str | Path, img_dir: str, doc_stem: str,
              extractor: str = "pymupdf4llm") -> tuple[str, int, int, int]:
    import pymupdf
    import pymupdf4llm

    from mianjing_kb.kb import pdfbudget
    doc = pymupdf.open(str(path))
    pdfbudget.check_page_count(len(doc))       # #65：文本抽取前的页数预算
    refs, kept, skipped = pdf_extract_images(doc, img_dir, doc_stem)
    if extractor == "pymupdf4llm":
        md = pymupdf4llm.to_markdown(doc, write_images=False, show_progress=False)
    else:  # pypdf fallback
        from pypdf import PdfReader
        import logging
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
        md = "\n\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    md = normalize_ascii_dot_space(md)
    pages = len(doc)
    doc.close()
    if refs:
        md += (f"\n\n---\n\n## 配图（{len(refs)}张）\n\n"
               + "\n".join(f"![配图 原文第{p}页](./images/{fn})\n" for fn, p in refs))
    return md, pages, kept, skipped


def corpus_root(explicit: str | None = None) -> Path:
    """语料根：显式参数 > KB_CORPUS_ROOT。仓内版本不按 __file__ 反推（会算成仓库根）。"""
    root = explicit or os.environ.get("KB_CORPUS_ROOT")
    if not root:
        raise SystemExit("需要 --corpus-root 或环境变量 KB_CORPUS_ROOT 指向语料根")
    return Path(root)


CAT_MAP = {"题库": "题库", "真题": "真题", "面经": "面经", "个人面经": "个人面经",
           "专题笔记": "专题笔记"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="单文件 → md（仓内固定版）")
    ap.add_argument("entry")
    ap.add_argument("result")
    ap.add_argument("--extractor", default="pymupdf4llm")
    ap.add_argument("--corpus-root", default=None)
    args = ap.parse_args(argv)
    root = corpus_root(args.corpus_root).resolve()
    with open(args.entry, encoding="utf-8") as f:
        e = json.load(f)
    res = dict(rel=e.get("rel"), category=e.get("category"), topic=e.get("topic"))

    def _dump() -> None:
        with open(args.result, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)

    try:
        src = paths.safe_join(root, str(e.get("rel") or ""), must_exist=True)
    except paths.UnsafePath as ex:
        res.update(ok=False, err=f"不安全源路径: {ex}")
        _dump()
        return 1
    category_value = e.get("category")
    category = CAT_MAP.get(category_value) if isinstance(category_value, str) else None
    if category is None:
        res.update(ok=False, err=f"非法 category: {e.get('category')!r}")
        _dump()
        return 1
    topic = safe_name(e.get("topic") or "") or "综合"
    title = PurePosixPath(str(e.get("title") or src.name).replace("\\", "/")).name
    stem = safe_name(PurePosixPath(title).stem)
    if not stem:
        res.update(ok=False, err="非法/空 title")
        _dump()
        return 1
    ext = str(e.get("ext") or src.suffix).lower()
    if ext != src.suffix.lower():
        res.update(ok=False, err=f"扩展名与源文件不匹配: {ext} != {src.suffix.lower()}")
        _dump()
        return 1
    mdroot = paths.safe_join(root, "20-题库MD")
    out_rel = Path(category) / topic / f"{stem}.md"
    try:
        md_path = paths.safe_join(mdroot, out_rel)
    except paths.UnsafePath as ex:
        res.update(ok=False, err=f"不安全输出路径: {ex}")
        _dump()
        return 1
    out_dir = md_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        images_dir = paths.safe_join(mdroot, out_rel.parent / "images")
        if ext == ".pdf":
            md, pages, kept, skipped = pdf_to_md(src, str(images_dir), stem, args.extractor)
            ratio = cjk_ratio(md)
            if ratio < 0.05:  # 质量门禁：中文占比过低 => 字体映射损坏
                res.update(ok=False, err=f"低CJK({ratio:.1%})", extractor=args.extractor)
                _dump()
                return 1
            atomic.atomic_write_text(md_path, md, encoding="utf-8")
            res.update(ok=True, chars=len(md), pages=pages, cjk=round(ratio, 3),
                       imgs_kept=kept, imgs_skipped=skipped, extractor=args.extractor)
        elif ext in (".docx", ".doc", ".txt", ".md"):
            if ext == ".docx":
                md = docx_to_md(src)
            elif ext == ".doc":
                # #84：本入口常以 `python -m mianjing_kb.kb.convert_one` 直跑，
                # 无父进程看门狗；进程内 COM 无法被打断，异常 .doc 会卡死。
                # 拒绝并指向有逐文件子进程 + 硬超时的 doc_convert 生产链路。
                res.update(ok=False, err=".doc 请先用 python -m mianjing_kb.doc_convert "
                                         "转成 .docx（逐文件子进程+超时隔离）再转换")
                _dump()
                return 1
            else:
                md = txt_to_text(src)
            if not md.strip():
                res.update(ok=False, err="空文本")
            else:
                atomic.atomic_write_text(md_path, md, encoding="utf-8")
                res.update(ok=True, chars=len(md), cjk=round(cjk_ratio(md), 3),
                           imgs_kept=0, imgs_skipped=0, extractor=ext)
        else:
            res.update(ok=False, err=f"不支持{ext}")
    except Exception as ex:                            # noqa: BLE001
        res.update(ok=False, err=f"{type(ex).__name__}: {str(ex)[:200]}", extractor=args.extractor)
    _dump()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
