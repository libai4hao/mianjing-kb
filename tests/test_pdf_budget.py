# -*- coding: utf-8 -*-
"""PDF 渲染/解析资源预算回归（审计候选 #43/#63/#65）。"""
import pymupdf
import pytest

from mianjing_kb import gate, ocr_pages
from mianjing_kb.kb import convert_one, pdfbudget


def _pdf_with_page(tmp_path, w_pt, h_pt, pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=w_pt, height=h_pt)
    p = tmp_path / "big.pdf"
    doc.save(str(p))
    doc.close()
    return p


def test_render_geometry_budget_math():
    w, h = pdfbudget.check_render_geometry(595, 842, 200)   # A4@200dpi
    assert w * h < pdfbudget.MAX_RENDER_PIXELS
    with pytest.raises(pdfbudget.ResourceBudgetError):
        pdfbudget.check_render_geometry(20000, 20000, 200)  # 巨幅页盒
    with pytest.raises(pdfbudget.ResourceBudgetError):
        pdfbudget.check_render_geometry(595, 842, 0)


def test_ocr_render_page_rejects_oversized_box(tmp_path):
    """#63：超大页盒在 get_pixmap 之前被拒绝（不分配巨型位图）。"""
    pdf = _pdf_with_page(tmp_path, 30000, 30000)
    with pytest.raises(pdfbudget.ResourceBudgetError):
        ocr_pages.render_page(pdf, 1, tmp_path / "p.png", dpi=200)
    assert not (tmp_path / "p.png").exists()


def test_ocr_render_page_normal_pdf_still_works(tmp_path):
    pdf = _pdf_with_page(tmp_path, 595, 842)
    out = ocr_pages.render_page(pdf, 1, tmp_path / "p.png", dpi=100)
    assert out.exists() and out.stat().st_size > 0


def test_gate_pix_fp_rejects_oversized_box(tmp_path):
    """#43：哨兵像素指纹同样先核算几何预算。"""
    pdf = _pdf_with_page(tmp_path, 30000, 30000)
    with pytest.raises(pdfbudget.ResourceBudgetError):
        gate.pix_fp(pdf)


def test_page_count_budget(tmp_path):
    with pytest.raises(pdfbudget.ResourceBudgetError):
        pdfbudget.check_page_count(pdfbudget.MAX_PDF_PAGES + 1)
    pdfbudget.check_page_count(10)


def test_image_decode_budget():
    pdfbudget.check_image_decode(1000, 1000, 4)
    with pytest.raises(pdfbudget.ResourceBudgetError):
        pdfbudget.check_image_decode(20000, 20000, 4)


def test_pdf_extract_images_skips_oversized_image(tmp_path):
    """#65：超大嵌入图按 xref 尺寸在解码前跳过，正常小图仍提取。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 正常小图（100x100 红色）
    small = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100))
    small.set_rect(small.irect, (255, 0, 0))
    page.insert_image(pymupdf.Rect(0, 0, 100, 100), pixmap=small)
    # 超大图：直接构造大尺寸 xref（不解码，靠 im[2]/im[3] 尺寸闸）
    big = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 9000, 9000))
    page.insert_image(pymupdf.Rect(100, 100, 200, 200), pixmap=big)
    refs, kept, skipped = convert_one.pdf_extract_images(doc, str(tmp_path / "imgs"), "t")
    doc.close()
    assert kept == 1 and skipped >= 1          # 小图保留，超大图被预算跳过
