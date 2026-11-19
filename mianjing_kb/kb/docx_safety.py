"""受资源上限约束的 DOCX ZIP 成员读取。"""
from __future__ import annotations

import zipfile
from pathlib import Path

MAX_DOCX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DOCX_ENTRIES = 10_000
MAX_DOCX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_DOCX_XML_BYTES = 32 * 1024 * 1024
MAX_DOCX_XML_RATIO = 250


class UnsafeDocx(ValueError):
    """DOCX 结构或解压资源超出安全限制。"""


def read_docx_member(path: str | Path, member: str, *,
                     max_bytes: int = MAX_DOCX_XML_BYTES) -> bytes:
    """读取指定 XML 成员前先检查 ZIP 元数据，并对实际读取字节数再次限额。"""
    try:
        archive_path = Path(path)
        if archive_path.stat().st_size > MAX_DOCX_ARCHIVE_BYTES:
            raise UnsafeDocx(
                f"DOCX 压缩包文件大小超限: {archive_path.stat().st_size} bytes")
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise UnsafeDocx(f"DOCX ZIP 条目过多: {len(infos)}")
            total = sum(info.file_size for info in infos)
            if total > MAX_DOCX_TOTAL_UNCOMPRESSED:
                raise UnsafeDocx(f"DOCX 解压总大小超限: {total} bytes")
            info = archive.getinfo(member)
            if info.flag_bits & 0x1:
                raise UnsafeDocx("不接受加密 DOCX 成员")
            if info.file_size > max_bytes:
                raise UnsafeDocx(f"{member} 解压大小超限: {info.file_size} bytes")
            if info.file_size > 1024 * 1024:
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > MAX_DOCX_XML_RATIO:
                    raise UnsafeDocx(f"{member} 压缩比异常: {ratio:.1f}")
            out = bytearray()
            with archive.open(info, "r") as src:
                while True:
                    chunk = src.read(min(1024 * 1024, max_bytes + 1 - len(out)))
                    if not chunk:
                        break
                    out.extend(chunk)
                    if len(out) > max_bytes:
                        raise UnsafeDocx(f"{member} 实际解压大小超限")
            return bytes(out)
    except UnsafeDocx:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as ex:
        raise UnsafeDocx(f"DOCX 成员读取失败: {member}: {ex}") from ex
