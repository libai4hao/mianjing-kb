# -*- coding: utf-8 -*-
"""PDF/图像渲染与解析的共享资源预算（审计候选 #43/#63/#65，P1-D）。

低信任 PDF 的页盒/图像尺寸由文件内容控制；渲染前必须先按几何尺寸核算
像素与解码字节，超限直接拒绝，而不是先分配再失败。所有阈值可用环境变量
覆盖（KB_PDF_MAX_*），默认值按生产语料实测留足余量：
- 最大渲染像素 40M（A4@200dpi ≈ 3.9M，超大幅面工程图也够用）；
- 单图解码字节 64MiB、单 PDF 图片累计 512MiB、图片张数 2000；
- 单 PDF 页数 5000（生产最大合订本 ~1500 页）。
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


MAX_RENDER_PIXELS = _env_int("KB_PDF_MAX_RENDER_PIXELS", 40_000_000)
MAX_IMAGE_DECODE_BYTES = _env_int("KB_PDF_MAX_IMAGE_BYTES", 64 * 1024 * 1024)
MAX_IMAGES_PER_DOC = _env_int("KB_PDF_MAX_IMAGES", 2000)
MAX_IMAGE_TOTAL_BYTES = _env_int("KB_PDF_MAX_IMAGE_TOTAL_BYTES", 512 * 1024 * 1024)
MAX_PDF_PAGES = _env_int("KB_PDF_MAX_PAGES", 5000)
MAX_TOC_ENTRIES = _env_int("KB_PDF_MAX_TOC", 10_000)


class ResourceBudgetError(ValueError):
    """渲染/解析资源超出预算；调用方应记失败并跳过该件，不继续分配。"""


def check_render_geometry(width_pt: float, height_pt: float, dpi: int,
                          *, max_pixels: int = MAX_RENDER_PIXELS) -> tuple[int, int]:
    """按页盒（pt）与 dpi 核算渲染尺寸；超限抛 ResourceBudgetError。

    返回 (width_px, height_px)。先算后渲染——get_pixmap 一旦调用就会分配。
    """
    if dpi <= 0 or dpi > 1200:
        raise ResourceBudgetError(f"dpi 超界: {dpi}")
    w = max(1, int(width_pt * dpi / 72))
    h = max(1, int(height_pt * dpi / 72))
    if w * h > max_pixels:
        raise ResourceBudgetError(
            f"渲染像素超限: {w}x{h}={w * h} > {max_pixels}（页盒 {width_pt:.0f}x{height_pt:.0f}pt @ {dpi}dpi）")
    return w, h


def check_image_decode(width: int, height: int, channels: int,
                       *, max_bytes: int = MAX_IMAGE_DECODE_BYTES) -> None:
    """解码位图字节预算（宽×高×通道）；超限抛 ResourceBudgetError。"""
    need = int(width) * int(height) * max(1, int(channels))
    if need > max_bytes:
        raise ResourceBudgetError(
            f"图像解码字节超限: {width}x{height}x{channels}={need} > {max_bytes}")


def check_page_count(n: int, *, max_pages: int = MAX_PDF_PAGES) -> None:
    if n > max_pages:
        raise ResourceBudgetError(f"PDF 页数超限: {n} > {max_pages}")
