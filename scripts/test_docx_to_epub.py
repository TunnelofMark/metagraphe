#!/usr/bin/env python3
"""Offline stdlib-only regression tests for scripts/docx_to_epub.py.

Covers the four real-world OCR-DOCX pitfalls:
  1. mc:AlternateContent (Choice + Fallback) deduplication
  2. Micro-fragment image filtering (size + pixel dimensions)
  3. Layout pseudo-table unwrapping vs real data table preservation
  4. CJK line-wrap space collapsing, running header / page number stripping, CIP extraction,
     and EPUB 3.3 compliance (including EPUBCheck 0/0/0 when available).

Usage:
  python3 scripts/test_docx_to_epub.py
"""

from __future__ import annotations

import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
import xml.etree.ElementTree as ET

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import docx_to_epub


def make_png(width: int, height: int, min_bytes: int = 0) -> bytes:
    """Create a valid minimal RGB PNG with exact dimensions and optional padding chunk."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xcc\xdd\xee" * width for _ in range(height))
    idat = zlib.compress(raw)
    chunks = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", ihdr), chunk(b"IDAT", idat)]
    if min_bytes > 0:
        cur = sum(len(c) for c in chunks) + 12
        if cur < min_bytes:
            pad = b"X" * (min_bytes - cur)
            chunks.append(chunk(b"tEXt", b"Pad\x00" + pad))
    chunks.append(chunk(b"IEND", b""))
    return b"".join(chunks)


def make_synthetic_ocr_docx(path: pathlib.Path) -> None:
    real_img = make_png(240, 180, min_bytes=5200)
    tiny_img = make_png(40, 14, min_bytes=600)

    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
</Relationships>
"""

    doc_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <!-- CIP / Copyright block -->
    <w:p><w:r><w:t>图书在版编目(CIP)数据</w:t></w:r></w:p>
    <w:p><w:r><w:t>测试老街/张三著.—大连：测试出版社，2020.5</w:t></w:r></w:p>
    <w:p><w:r><w:t>ISBN 978-7-5505-0000-0</w:t></w:r></w:p>

    <!-- Genuine compact data table (must stay <table>) -->
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>主任：甲</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>主编单位</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>委员：乙</w:t></w:r></w:p></w:tc>
        <w:tc><w:r><w:t>协办单位</w:t></w:r></w:tc>
      </w:tr>
    </w:tbl>

    <!-- Chapter 1 -->
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:rPr><w:sz w:val="36"/></w:rPr><w:t>第一章 起点</w:t></w:r></w:p>

    <!-- Running header & standalone page number (must be dropped) -->
    <w:p><w:r><w:t>测试老街</w:t></w:r></w:p>
    <w:p><w:r><w:t>001</w:t></w:r></w:p>

    <!-- CJK line-wrap spaces + English phrase spacing preserved -->
    <w:p><w:r><w:t>大 连 最 早 的 一 条 街，名为 Old Street 遗址。</w:t></w:r></w:p>

    <!-- mc:AlternateContent with Choice and Fallback referencing the same real image + tiny decorative slice -->
    <w:p>
      <mc:AlternateContent>
        <mc:Choice><w:drawing><a:blip r:embed="rId1"/></w:drawing></mc:Choice>
        <mc:Fallback><w:drawing><a:blip r:embed="rId1"/></w:drawing></mc:Fallback>
      </mc:AlternateContent>
      <w:drawing><a:blip r:embed="rId2"/></w:drawing>
    </w:p>
    <w:p><w:r><w:t>今日老街(摄于2020年5月1日)</w:t></w:r></w:p>

    <!-- Body layout pseudo-table wrapping image + long prose (must be unwrapped, NOT <table>) -->
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>这 是一段被PDF转换器塞进伪表格里的正文内容，包含多个句子与历史细节，用来验证解包逻辑能否去掉多余边框并清洗中文字间空格，同时保留完整叙述。</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>

    <!-- Chapter 2 -->
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:rPr><w:sz w:val="36"/></w:rPr><w:t>后记</w:t></w:r></w:p>
    <w:p><w:r><w:t>写于海滨。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/_rels/document.xml.rels", rels_xml)
        z.writestr("word/media/image1.png", real_img)
        z.writestr("word/media/image2.png", tiny_img)


def run_tests() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        docx_path = tmp / "sample.docx"
        cover_path = tmp / "cover.png"
        epub_path = tmp / "sample.epub"

        make_synthetic_ocr_docx(docx_path)
        cover_path.write_bytes(make_png(300, 420, min_bytes=6000))

        # 1. Inspect mode
        report = docx_to_epub.inspect_docx(docx_path)
        assert report["cip_metadata"]["title"] == "测试老街", report
        assert report["cip_metadata"]["author"] == "张三", report
        assert report["cip_metadata"]["publisher"] == "测试出版社", report
        assert report["cip_metadata"]["isbn"] == "9787550500000", report
        assert report["images"]["total"] == 2 and report["images"]["kept"] == 1, report
        print("[PASS] inspect_docx: CIP extraction + micro-image filtering")

        # 2. Convert to EPUB 3.3
        res = docx_to_epub.convert_docx_to_epub(docx_path, epub_path, cover_image=cover_path)
        assert res["title"] == "测试老街", res
        assert res["embedded_images"] == 1, res
        assert res["sections"] == 3, res  # front_matter + ch01 + ch02

        with zipfile.ZipFile(epub_path, "r") as ez:
            infos = ez.infolist()
            assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
            assert ez.read("mimetype") == b"application/epub+zip"
            for name in ez.namelist():
                if name.endswith((".xml", ".xhtml", ".opf", ".ncx")):
                    ET.fromstring(ez.read(name))

            front_html = ez.read("OEBPS/text/front_matter.xhtml").decode("utf-8")
            assert "<table>" in front_html and "主任：甲" in front_html, front_html

            ch1_html = ez.read("OEBPS/text/ch01.xhtml").decode("utf-8")
            # CJK spaces collapsed, English phrase space preserved
            assert "大连最早的一条街，名为 Old Street 遗址。" in ch1_html, ch1_html
            # Running header & page number stripped
            assert "<p>001</p>" not in ch1_html, ch1_html
            assert "<p>测试老街</p>" not in ch1_html, ch1_html
            # mc:Fallback skipped (2 placements total: 1 in Choice, 1 in unwrapped layout table; NOT 3)
            assert ch1_html.count("<figure>") == 2, ch1_html
            # Layout pseudo-table unwrapped (no <table> in ch01)
            assert "<table>" not in ch1_html, ch1_html
            assert "这是一段被PDF转换器塞进伪表格里的正文内容" in ch1_html, ch1_html
            # Caption styled
            assert '<p class="caption">今日老街(摄于2020年5月1日)</p>' in ch1_html, ch1_html
        print("[PASS] convert_docx_to_epub: Fallback dedup + table unwrap + CJK cleanup + EPUB packaging")

        # 3. Run EPUBCheck if installed
        epubcheck = pathlib.Path("/opt/ocr/epubcheck/epubcheck.jar")
        if epubcheck.exists() and shutil.which("java"):
            proc = subprocess.run(
                ["java", "-jar", str(epubcheck), str(epub_path)],
                capture_output=True,
                text=True,
            )
            combined = proc.stdout + "\n" + proc.stderr
            assert proc.returncode == 0, f"EPUBCheck failed:\n{combined}"
            assert "0 fatals / 0 errors / 0 warnings" in combined, f"EPUBCheck warnings:\n{combined}"
            print("[PASS] EPUBCheck 3.3 validation: 0 fatals / 0 errors / 0 warnings")
        else:
            print("[SKIP] EPUBCheck not installed in this environment")


if __name__ == "__main__":
    run_tests()
