#!/usr/bin/env python3
"""Pure-CPU, zero-dependency fast path: convert an OCR-generated .docx to EPUB 3.3.

Designed for Chinese scanned book collections that already include a usable OCR .docx
(e.g., produced by ABBYY FineReader, WPS, or Adobe Acrobat), optionally paired with the
original scanned PDF to extract a crisp cover image.

Handles the four most common OCR-DOCX pitfalls:
  1. mc:AlternateContent (mc:Choice / mc:Fallback) & nested w:txbxContent deduplication
  2. Decorative micro-image filtering (size + pixel dimensions)
  3. Layout pseudo-table (w:tbl) unwrapping vs genuine data tables
  4. Running header / standalone page-number / art-font opener cleanup & CJK space collapsing

Usage:
  # 1. Read-only preflight check to see if an existing .docx is usable
  python3 scripts/docx_to_epub.py --inspect /path/to/book.docx

  # 2. Direct conversion to EPUB 3.3 (optionally extracting page 1 of source PDF as cover)
  python3 scripts/docx_to_epub.py /path/to/book.docx /path/to/book.epub \\
    --cover-pdf /path/to/book.pdf

  # 3. Conversion with explicit chapter map JSON (ideal for stylized art-font chapter openers)
  python3 scripts/docx_to_epub.py /path/to/book.docx /path/to/book.epub \\
    --cover-pdf /path/to/book.pdf --chapters chapters.json
"""

from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from typing import Any

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

CN_NUM = "一二三四五六七八九十百千万〇零两"
CHAPTER_RE = re.compile(
    rf"^(?:第[{CN_NUM}\d]+[编卷部章节回讲篇]|(?:序|序言|前言|引言|导言|绪论|后记|跋|附录|参考文献)(?:[\s：:].*)?)$"
)

DEFAULT_CSS = """@charset "UTF-8";
body {
  font-family: "Songti SC", "STSong", "Noto Serif CJK SC", "Source Han Serif SC", "SimSun", serif;
  line-height: 1.75;
  margin: 1em 1.2em;
  color: #222;
  text-align: justify;
}
h1 {
  font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  font-size: 1.5em;
  font-weight: bold;
  line-height: 1.35;
  margin: 0.8em 0 1.2em 0;
  padding-bottom: 0.4em;
  border-bottom: 1px solid #ccc;
  text-align: left;
  page-break-after: avoid;
}
h2 {
  font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  font-size: 1.15em;
  font-weight: bold;
  margin: 1.4em 0 0.6em 0;
  text-align: left;
  page-break-after: avoid;
}
p {
  text-indent: 2em;
  margin: 0.4em 0;
}
p.no-indent {
  text-indent: 0;
  margin: 0.35em 0;
}
p.caption {
  text-indent: 0;
  text-align: center;
  font-size: 0.85em;
  color: #555;
  margin: 0.2em 0 1em 0;
}
p.footnote {
  text-indent: 0;
  font-size: 0.88em;
  color: #444;
  margin: 0.5em 0;
  padding-left: 1em;
  border-left: 2px solid #ccc;
}
figure {
  margin: 1em 0 0.3em 0;
  text-align: center;
  page-break-inside: avoid;
}
figure img {
  max-width: 100%;
  height: auto;
  display: inline-block;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 0.92em;
}
td, th {
  border: 1px solid #ddd;
  padding: 0.4em 0.6em;
  vertical-align: top;
}
.cover-page {
  margin: 0;
  padding: 0;
  text-align: center;
}
.cover-page img {
  max-width: 100%;
  max-height: 100vh;
  height: auto;
}
"""


def img_dims(data: bytes, ext: str) -> tuple[int, int]:
    """Read (width, height) from PNG, JPEG, or GIF headers without external libraries."""
    ext = ext.lower().lstrip(".")
    if ext == "png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if ext == "gif" and len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)
    if ext in ("jpg", "jpeg") and len(data) >= 4 and data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i < n - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            while i < n and data[i] == 0xFF:
                i += 1
            if i >= n:
                break
            marker = data[i]
            i += 1
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                continue
            if i + 2 > n:
                break
            seg_len = struct.unpack(">H", data[i : i + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 7 <= n:
                    h, w = struct.unpack(">HH", data[i + 3 : i + 7])
                    return int(w), int(h)
                break
            if seg_len < 2:
                break
            i += seg_len
    return 0, 0


def extract_direct_p(p: ET.Element) -> tuple[str, int, bool, str]:
    """Extract (text, max_font_half_pts, is_mostly_bold, style_name) from direct runs of w:p.

    Intentionally excludes nested w:txbxContent and mc:Fallback to prevent duplicate text.
    """
    style_name = ""
    pPr = p.find(W + "pPr")
    if pPr is not None:
        pStyle = pPr.find(W + "pStyle")
        if pStyle is not None:
            style_name = pStyle.attrib.get(W + "val", "")

    texts: list[str] = []
    sizes: list[int] = []
    bold_chars = 0
    total_chars = 0

    for child in p:
        if child.tag != W + "r":
            continue
        rPr = child.find(W + "rPr")
        sz: int | None = None
        is_bold = False
        if rPr is not None:
            sz_el = rPr.find(W + "sz")
            if sz_el is not None:
                try:
                    sz = int(sz_el.attrib.get(W + "val", "0"))
                except ValueError:
                    sz = None
            b_el = rPr.find(W + "b")
            if b_el is not None and b_el.attrib.get(W + "val", "1") not in ("0", "false"):
                is_bold = True
        for sub in child:
            if sub.tag == W + "t" and sub.text:
                texts.append(sub.text)
                n = len(sub.text.strip())
                total_chars += n
                if is_bold:
                    bold_chars += n
                if sz:
                    sizes.append(sz)
            elif sub.tag == W + "tab":
                texts.append(" ")
            elif sub.tag == W + "br":
                texts.append(" ")

    text = "".join(texts).strip()
    max_sz = max(sizes) if sizes else 0
    is_mostly_bold = total_chars > 0 and (bold_chars / total_chars) >= 0.7
    return text, max_sz, is_mostly_bold, style_name


def extract_images(el: ET.Element, rels: dict[str, str], valid_media: dict[str, str]) -> list[str]:
    """Recursively collect valid media filenames referenced by el, skipping mc:Fallback."""
    imgs: list[str] = []
    for child in el:
        if child.tag == MC + "Fallback":
            continue
        if child.tag == A + "blip":
            rid = child.attrib.get(R + "embed")
            if rid and rid in rels:
                target = rels[rid]
                if not target.startswith("word/"):
                    target = "word/" + target.lstrip("/")
                if target in valid_media:
                    imgs.append(valid_media[target])
        else:
            imgs.extend(extract_images(child, rels, valid_media))
    return imgs


def clean_cjk_text(text: str, preserve_spaces: bool = False) -> str:
    """Collapse OCR line-wrap spaces between CJK characters while keeping English word spaces."""
    text = text.replace("\u3000", " ").strip()
    if not preserve_spaces:
        cjk = r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef·“”‘’《》【】（）]"
        for _ in range(2):
            text = re.sub(rf"(?<={cjk})\s+(?={cjk})", "", text)
        text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def is_garbled_art_ocr(text: str) -> bool:
    """Detect heavily fragmented single-character OCR gibberish from stylized art-font openers."""
    tokens = text.split()
    if len(tokens) >= 6 and sum(1 for tok in tokens if len(tok) == 1) / len(tokens) > 0.7:
        return True
    return False


def is_noise_text(
    text: str,
    sz: int,
    idx: int,
    ch_start: int,
    book_title: str = "",
) -> bool:
    """Filter standalone page numbers, running book-title headers, and art-font opener noise."""
    if not text:
        return True
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    # Standalone page numbers (1..9999)
    if re.fullmatch(r"\d{1,4}", compact):
        return True
    # Pure punctuation / divider noise
    if re.fullmatch(r"[\W_·•…\-—=+|\\/\d]+", compact):
        return True
    # Running header matching book title
    if book_title:
        clean_bt = re.sub(r"[\s·•\-—:：]", "", book_title)
        clean_cp = re.sub(r"[\s·•\-—:：]", "", compact)
        if clean_bt and clean_cp == clean_bt and len(clean_cp) <= 24:
            return True
    # Art-font opener noise within the first few elements of a chapter
    if idx - ch_start <= 4:
        if is_garbled_art_ocr(text):
            return True
        if idx - ch_start <= 3 and (sz >= 56 or len(compact) <= 2):
            return True
    return False


def is_caption(text: str) -> bool:
    if len(text) > 75:
        return False
    patterns = [
        r"[（(]摄于\d{4}",
        r"/摄[)）]",
        r"/作[)）]",
        r"^图\s*\d",
        r"^老明信片上的",
        r"老地图",
        r"手绘地图",
        r"略图$",
        r"^\d{4}年的",
        r"^20世纪\d+年代的",
    ]
    return any(re.search(p, text) for p in patterns)


def is_subheading(text: str, is_bold: bool, sz: int, style_name: str, prev_was_img: bool) -> bool:
    if not text or len(text) > 28:
        return False
    if is_caption(text):
        return False
    if text[-1] in "。，、；：！？\"”’）)……":
        return False
    if text.startswith(("【", "[")):
        return False
    if style_name.lower().startswith(("heading2", "heading3", "2", "3")):
        return True
    # Decorative bullet icon OCR'd as leading "Y " or "V "
    if re.match(r"^[YV]\s+[\u4e00-\u9fff]", text):
        return True
    if text == "补遗":
        return True
    if (is_bold or sz >= 24) and len(text) <= 22 and not prev_was_img:
        return True
    return False


def is_real_data_table(tbl_el: ET.Element, rels: dict[str, str], valid_media: dict[str, str]) -> bool:
    """Distinguish genuine structured data tables from PDF-to-DOCX layout wrapper tables.

    PDF-to-DOCX converters frequently wrap side-by-side image+prose blocks or chapter opener
    art-font boxes in w:tbl. Genuine tables typically have >= 2 rows, >= 2 columns, no
    embedded body photos, and relatively short cell text (not multi-sentence prose).
    """
    if extract_images(tbl_el, rels, valid_media):
        return False
    rows = tbl_el.findall(W + "tr")
    if len(rows) < 2:
        return False
    col_counts = []
    cell_lengths = []
    for tr in rows:
        tcs = tr.findall(W + "tc")
        col_counts.append(len(tcs))
        for tc in tcs:
            ct = " ".join(extract_direct_p(p)[0] for p in tc.findall(W + "p") if extract_direct_p(p)[0])
            if ct:
                cell_lengths.append(len(ct))
    if not cell_lengths or max(col_counts) < 2:
        return False
    # Reject tables whose cells contain long prose paragraphs
    if max(cell_lengths) > 90 or (sum(cell_lengths) / len(cell_lengths)) > 45:
        return False
    return True


def extract_cip_metadata(children: list[ET.Element], max_scan: int = 200) -> dict[str, str]:
    """Best-effort extraction of title, author, publisher, date, and ISBN from front matter."""
    meta: dict[str, str] = {}
    lines: list[str] = []
    for el in children[:max_scan]:
        if el.tag == W + "p":
            txt = clean_cjk_text(extract_direct_p(el)[0], preserve_spaces=True)
            if txt:
                lines.append(txt)
    joined = "\n".join(lines)

    m_isbn = re.search(r"ISBN\s*([0-9Xx\-]{10,17})", joined)
    if m_isbn:
        meta["isbn"] = re.sub(r"[^0-9Xx]", "", m_isbn.group(1))

    # Typical Chinese CIP line: 书名/作者著.—出版地：出版社，YYYY.M
    for line in lines:
        m_cip = re.match(
            r"^(.{2,40}?)\s*/\s*([^.。]{1,25}?)\s*[.。]\s*[—\-一]+\s*[^：:]{1,12}[：:]\s*([^，,\s]{2,20})[，,]\s*(\d{4}(?:\.\d{1,2})?)",
            line,
        )
        if m_cip:
            meta.setdefault("title", clean_cjk_text(m_cip.group(1)))
            author_raw = clean_cjk_text(m_cip.group(2))
            author_clean = re.sub(r"(编著|主编|编译|译注|整理|著|编|译)$", "", author_raw).strip()
            meta.setdefault("author", author_clean or author_raw)
            meta.setdefault("publisher", clean_cjk_text(m_cip.group(3)))
            meta.setdefault("date", m_cip.group(4).replace(".", "-"))
            break

    m_pub = re.search(r"出版(?:发行)?(?:者|社)?\s*[：:]\s*([^\n]{2,24})", joined)
    if m_pub and "publisher" not in meta:
        meta["publisher"] = clean_cjk_text(m_pub.group(1))

    return meta


def filter_valid_media(
    z: zipfile.ZipFile,
    min_bytes: int = 4000,
    min_dim: int = 60,
    min_long_edge: int = 100,
) -> tuple[dict[str, str], dict[str, tuple[bytes, str]], dict[str, int]]:
    """Keep real photos/illustrations and drop tiny decorative divider/header fragments."""
    valid_media: dict[str, str] = {}
    media_bytes: dict[str, tuple[bytes, str]] = {}
    stats = {"total": 0, "dropped_small": 0, "dropped_dims": 0, "kept": 0}
    counter = 0

    for info in z.infolist():
        if not info.filename.startswith("word/media/") or info.is_dir():
            continue
        stats["total"] += 1
        ext = info.filename.rsplit(".", 1)[-1].lower() if "." in info.filename else ""
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            stats["dropped_small"] += 1
            continue
        if info.file_size < min_bytes:
            stats["dropped_small"] += 1
            continue
        data = z.read(info.filename)
        w, h = img_dims(data, ext)
        if w > 0 and h > 0 and (min(w, h) < min_dim or max(w, h) < min_long_edge):
            stats["dropped_dims"] += 1
            continue
        counter += 1
        norm_ext = "jpg" if ext == "jpeg" else ext
        mime = "image/jpeg" if norm_ext == "jpg" else f"image/{norm_ext}"
        out_name = f"img_{counter:04d}.{norm_ext}"
        valid_media[info.filename] = out_name
        media_bytes[out_name] = (data, mime)
        stats["kept"] += 1

    return valid_media, media_bytes, stats


def detect_chapters_auto(children: list[ET.Element], book_title: str = "") -> list[dict[str, Any]]:
    """Auto-detect chapter boundaries using Word Heading styles, Chinese chapter regex, or large font."""
    candidates: list[tuple[int, str, str]] = []  # (idx, title, source)

    # Pass 1: Word Heading 1 styles
    for idx, el in enumerate(children):
        if el.tag != W + "p":
            continue
        txt, sz, _, style = extract_direct_p(el)
        cleaned = clean_cjk_text(txt)
        if not cleaned or len(cleaned) > 45:
            continue
        if style.lower() in ("heading1", "1", "title") or style in ("标题 1", "标题1"):
            candidates.append((idx, cleaned, "style"))

    # Pass 2: Chinese structural headings (第X章 / 序 / 后记 / 参考文献)
    if len(candidates) < 2:
        candidates = []
        for idx, el in enumerate(children):
            if el.tag != W + "p":
                continue
            txt, sz, is_bold, _ = extract_direct_p(el)
            cleaned = clean_cjk_text(txt)
            if not cleaned or len(cleaned) > 40:
                continue
            if CHAPTER_RE.match(cleaned) and (sz >= 22 or is_bold or sz == 0):
                candidates.append((idx, cleaned, "regex"))

    # Pass 3: Fallback to single chapter if no reliable headings detected
    if not candidates:
        return [{"start": 0, "id": "ch01", "title": book_title or "正文"}]

    chapters: list[dict[str, Any]] = []
    if candidates[0][0] > 0:
        chapters.append({"start": 0, "id": "front_matter", "title": "卷首"})
    for i, (idx, title, _) in enumerate(candidates, start=1):
        chapters.append({"start": idx, "id": f"ch{i:02d}", "title": title})
    return chapters


def extract_cover_from_pdf(pdf_path: pathlib.Path, dpi: int = 150) -> bytes:
    """Render page 1 of a scanned PDF as a JPEG cover using pdftoppm."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm not found; install poppler-utils or pass --cover-image instead.")
    with tempfile.TemporaryDirectory() as td:
        prefix = os.path.join(td, "cover")
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), "-f", "1", "-l", "1", str(pdf_path), prefix],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        matches = sorted(pathlib.Path(td).glob("cover*.jpg"))
        if not matches:
            raise RuntimeError(f"pdftoppm produced no cover image for {pdf_path}")
        return matches[0].read_bytes()


def wrap_xhtml(title: str, body_inner: str, body_class: str = "") -> str:
    bcls = f' class="{body_class}"' if body_class else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="UTF-8"/>\n'
        f"  <title>{html.escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>\n'
        "</head>\n"
        f"<body{bcls}>\n"
        f"{body_inner}\n"
        "</body>\n"
        "</html>\n"
    )


def inspect_docx(docx_path: pathlib.Path, min_bytes: int = 4000, min_dim: int = 60) -> dict[str, Any]:
    """Read-only preflight inspection of a .docx file to assess OCR quality and structure."""
    with zipfile.ZipFile(docx_path, "r") as z:
        _, _, img_stats = filter_valid_media(z, min_bytes=min_bytes, min_dim=min_dim)
        root = ET.fromstring(z.read("word/document.xml"))
        body = root.find(W + "body")
        assert body is not None, "Invalid docx: missing w:body"
        children = list(body)

        tag_counts = Counter(el.tag.split("}")[-1] for el in children)
        non_empty_paras = 0
        total_chars = 0
        large_headings: list[tuple[int, int, str]] = []
        for idx, el in enumerate(children):
            if el.tag != W + "p":
                continue
            txt, sz, _, style = extract_direct_p(el)
            cleaned = clean_cjk_text(txt)
            if cleaned:
                non_empty_paras += 1
                total_chars += len(cleaned)
                if (sz >= 32 or style.lower().startswith("heading") or CHAPTER_RE.match(cleaned)) and len(cleaned) <= 50:
                    large_headings.append((idx, sz, cleaned))

        cip = extract_cip_metadata(children)
        auto_ch = detect_chapters_auto(children, book_title=cip.get("title", ""))

    return {
        "path": str(docx_path),
        "body_elements": len(children),
        "top_tags": dict(tag_counts),
        "non_empty_paragraphs": non_empty_paras,
        "total_chars": total_chars,
        "images": img_stats,
        "cip_metadata": cip,
        "auto_chapters_count": len(auto_ch),
        "sample_headings": large_headings[:25],
    }


def convert_docx_to_epub(
    docx_path: pathlib.Path,
    output_epub: pathlib.Path,
    *,
    cover_pdf: pathlib.Path | None = None,
    cover_image: pathlib.Path | None = None,
    title: str | None = None,
    author: str | None = None,
    publisher: str | None = None,
    date: str | None = None,
    isbn: str | None = None,
    chapters_spec: list[dict[str, Any]] | None = None,
    skip_ranges: list[tuple[int, int]] | None = None,
    min_img_bytes: int = 4000,
    min_img_dim: int = 60,
) -> dict[str, Any]:
    """Convert an OCR-generated .docx into a clean EPUB 3.3 package."""
    skip_ranges = skip_ranges or []

    with zipfile.ZipFile(docx_path, "r") as z:
        rels_root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        rels = {el.attrib["Id"]: el.attrib["Target"] for el in rels_root}

        valid_media, media_bytes, img_stats = filter_valid_media(
            z, min_bytes=min_img_bytes, min_dim=min_img_dim
        )

        root = ET.fromstring(z.read("word/document.xml"))
        body = root.find(W + "body")
        assert body is not None, "Invalid docx: missing w:body"
        children = list(body)
        total_elems = len(children)

        cip = extract_cip_metadata(children)
        book_title = title or cip.get("title") or docx_path.stem
        book_author = author or cip.get("author") or "未知作者"
        book_publisher = publisher or cip.get("publisher") or ""
        book_date = date or cip.get("date") or ""
        book_isbn = isbn or cip.get("isbn") or ""

        chapters = chapters_spec or detect_chapters_auto(children, book_title=book_title)
        chapters = sorted(chapters, key=lambda c: int(c["start"]))

        used_images: set[str] = set()
        chapter_docs: list[tuple[str, str, str, dict[str, int]]] = []

        for ci, ch in enumerate(chapters):
            start_idx = int(ch["start"])
            default_end = int(chapters[ci + 1]["start"]) if ci + 1 < len(chapters) else total_elems
            end_idx = int(ch.get("end", default_end))
            file_id = str(ch.get("id") or f"ch{ci + 1:02d}")
            ch_title = str(ch.get("title") or f"第{ci + 1}章")
            preserve_spaces = bool(ch.get("preserve_spaces", False)) or file_id in (
                "front_copyright",
                "front_committee",
                "back_references",
            )

            blocks: list[str] = []
            para_count = 0
            img_count = 0
            char_count = 0
            prev_was_img = False

            for idx in range(start_idx, min(end_idx, total_elems)):
                if any(s <= idx <= e for s, e in skip_ranges):
                    continue
                el = children[idx]

                if el.tag == W + "tbl":
                    if is_real_data_table(el, rels, valid_media):
                        rows_html: list[str] = []
                        for tr in el.findall(W + "tr"):
                            cells_html: list[str] = []
                            for tc in tr.findall(W + "tc"):
                                ct = " ".join(
                                    clean_cjk_text(extract_direct_p(p)[0], preserve_spaces=True)
                                    for p in tc.findall(W + "p")
                                    if extract_direct_p(p)[0]
                                )
                                cells_html.append(f"<td>{html.escape(ct)}</td>")
                            if any(c != "<td></td>" for c in cells_html):
                                rows_html.append("<tr>" + "".join(cells_html) + "</tr>")
                        if rows_html:
                            blocks.append("<table>\n" + "\n".join(rows_html) + "\n</table>")
                    else:
                        # Unwrap layout pseudo-table: keep real images; skip garbled opener tables
                        is_opener_tbl = (idx - start_idx <= 2) and ci > 0
                        for im in extract_images(el, rels, valid_media):
                            used_images.add(im)
                            blocks.append(f'<figure><img src="../images/{im}" alt=""/></figure>')
                            img_count += 1
                            prev_was_img = True
                        if not is_opener_tbl:
                            for p in el.findall(".//" + W + "p"):
                                raw_t, t_sz, _, _ = extract_direct_p(p)
                                if not raw_t or is_noise_text(raw_t, t_sz, idx, start_idx, book_title):
                                    continue
                                cleaned = clean_cjk_text(raw_t, preserve_spaces=preserve_spaces)
                                if not cleaned:
                                    continue
                                if is_caption(cleaned):
                                    blocks.append(f'<p class="caption">{html.escape(cleaned)}</p>')
                                else:
                                    blocks.append(f"<p>{html.escape(cleaned)}</p>")
                                para_count += 1
                                char_count += len(cleaned)
                                prev_was_img = False
                    prev_was_img = False
                    continue

                if el.tag != W + "p":
                    continue

                for im in extract_images(el, rels, valid_media):
                    used_images.add(im)
                    blocks.append(f'<figure><img src="../images/{im}" alt=""/></figure>')
                    img_count += 1
                    prev_was_img = True

                raw_text, sz, is_bold, style_name = extract_direct_p(el)
                if not raw_text:
                    continue
                if is_noise_text(raw_text, sz, idx, start_idx, book_title):
                    continue

                cleaned = clean_cjk_text(raw_text, preserve_spaces=preserve_spaces)
                if not cleaned:
                    continue
                # Skip duplicate chapter heading right at chapter start
                if idx == start_idx and clean_cjk_text(ch_title) == cleaned:
                    continue

                if re.match(r"^【\d+】", cleaned):
                    blocks.append(f'<p class="footnote">{html.escape(cleaned)}</p>')
                    para_count += 1
                    char_count += len(cleaned)
                    prev_was_img = False
                elif is_caption(cleaned) and (prev_was_img or "摄于" in cleaned or "/作" in cleaned or "/摄" in cleaned):
                    blocks.append(f'<p class="caption">{html.escape(cleaned)}</p>')
                    para_count += 1
                    char_count += len(cleaned)
                    prev_was_img = False
                elif not preserve_spaces and is_subheading(cleaned, is_bold, sz, style_name, prev_was_img):
                    sub_title = re.sub(r"^[YV]\s+", "", cleaned)
                    blocks.append(f"<h2>{html.escape(sub_title)}</h2>")
                    para_count += 1
                    char_count += len(sub_title)
                    prev_was_img = False
                else:
                    cls = ' class="no-indent"' if preserve_spaces else ""
                    blocks.append(f"<p{cls}>{html.escape(cleaned)}</p>")
                    para_count += 1
                    char_count += len(cleaned)
                    prev_was_img = False

            if not blocks:
                continue
            body_html = f"<h1>{html.escape(ch_title)}</h1>\n" + "\n".join(blocks)
            chapter_docs.append(
                (file_id, ch_title, body_html, {"paras": para_count, "images": img_count, "chars": char_count})
            )

    # Optional cover bytes
    cover_bytes: bytes | None = None
    cover_mime = "image/jpeg"
    cover_ext = "jpg"
    if cover_image is not None:
        cover_bytes = cover_image.read_bytes()
        ext = cover_image.suffix.lower().lstrip(".")
        cover_ext = "jpg" if ext in ("jpg", "jpeg") else ext
        cover_mime = "image/jpeg" if cover_ext == "jpg" else f"image/{cover_ext}"
    elif cover_pdf is not None:
        cover_bytes = extract_cover_from_pdf(cover_pdf)

    # Build EPUB 3.3 + NCX package
    book_uuid = str(uuid.uuid4())
    modified_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    nav_items: list[str] = []
    ncx_points: list[str] = []
    manifest_items = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="styles/style.css" media-type="text/css"/>',
    ]
    spine_items: list[str] = []
    play_order = 1

    if cover_bytes is not None:
        manifest_items.append(
            f'    <item id="cover-image" href="images/cover.{cover_ext}" media-type="{cover_mime}" properties="cover-image"/>'
        )
        manifest_items.append(
            '    <item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine_items.append('    <itemref idref="cover"/>')
        nav_items.append('      <li><a href="text/cover.xhtml">封面</a></li>')
        ncx_points.append(
            f'    <navPoint id="navPoint-0" playOrder="{play_order}">\n'
            "      <navLabel><text>封面</text></navLabel>\n"
            '      <content src="text/cover.xhtml"/>\n'
            "    </navPoint>"
        )
        play_order += 1

    for idx_ch, (fid, ch_title, _, _) in enumerate(chapter_docs, start=1):
        manifest_items.append(f'    <item id="{fid}" href="text/{fid}.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.append(f'    <itemref idref="{fid}"/>')
        nav_items.append(f'      <li><a href="text/{fid}.xhtml">{html.escape(ch_title)}</a></li>')
        ncx_points.append(
            f'    <navPoint id="navPoint-{idx_ch}" playOrder="{play_order}">\n'
            f"      <navLabel><text>{html.escape(ch_title)}</text></navLabel>\n"
            f'      <content src="text/{fid}.xhtml"/>\n'
            "    </navPoint>"
        )
        play_order += 1

    for im in sorted(used_images):
        _, mime = media_bytes[im]
        item_id = "img-" + im.replace(".", "-")
        manifest_items.append(f'    <item id="{item_id}" href="images/{im}" media-type="{mime}"/>')

    nav_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="UTF-8"/>\n'
        "  <title>目录</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/style.css"/>\n'
        "</head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        "    <h1>目录</h1>\n"
        "    <ol>\n"
        + "\n".join(nav_items)
        + "\n    </ol>\n"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )

    toc_ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="urn:uuid:{book_uuid}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{html.escape(book_title)}</text></docTitle>\n"
        f"  <docAuthor><text>{html.escape(book_author)}</text></docAuthor>\n"
        "  <navMap>\n"
        + "\n".join(ncx_points)
        + "\n  </navMap>\n"
        "</ncx>\n"
    )

    meta_lines = [
        f'    <dc:identifier id="bookid">urn:uuid:{book_uuid}</dc:identifier>',
    ]
    if book_isbn:
        meta_lines.append(f"    <dc:identifier>urn:isbn:{html.escape(book_isbn)}</dc:identifier>")
    meta_lines.extend(
        [
            f"    <dc:title>{html.escape(book_title)}</dc:title>",
            f"    <dc:creator>{html.escape(book_author)}</dc:creator>",
        ]
    )
    if book_publisher:
        meta_lines.append(f"    <dc:publisher>{html.escape(book_publisher)}</dc:publisher>")
    meta_lines.append("    <dc:language>zh-CN</dc:language>")
    if book_date:
        meta_lines.append(f"    <dc:date>{html.escape(book_date)}</dc:date>")
    meta_lines.append(f'    <meta property="dcterms:modified">{modified_ts}</meta>')
    if cover_bytes is not None:
        meta_lines.append('    <meta name="cover" content="cover-image"/>')

    guide_lines = []
    if cover_bytes is not None:
        guide_lines.append('    <reference type="cover" title="封面" href="text/cover.xhtml"/>')
    guide_lines.append('    <reference type="toc" title="目录" href="nav.xhtml"/>')

    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        + "\n".join(meta_lines)
        + "\n  </metadata>\n"
        "  <manifest>\n"
        + "\n".join(manifest_items)
        + "\n  </manifest>\n"
        '  <spine toc="ncx">\n'
        + "\n".join(spine_items)
        + "\n  </spine>\n"
        "  <guide>\n"
        + "\n".join(guide_lines)
        + "\n  </guide>\n"
        "</package>\n"
    )

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )

    output_epub.parent.mkdir(parents=True, exist_ok=True)
    tmp_epub = output_epub.with_suffix(output_epub.suffix + ".tmp")
    with zipfile.ZipFile(tmp_epub, "w") as ez:
        ez.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        ez.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        ez.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
        ez.writestr("OEBPS/toc.ncx", toc_ncx, compress_type=zipfile.ZIP_DEFLATED)
        ez.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        ez.writestr("OEBPS/styles/style.css", DEFAULT_CSS, compress_type=zipfile.ZIP_DEFLATED)
        if cover_bytes is not None:
            cover_xhtml = wrap_xhtml(
                "封面",
                f'<div class="cover-page"><img src="../images/cover.{cover_ext}" alt="{html.escape(book_title)}"/></div>',
                body_class="cover-page",
            )
            ez.writestr("OEBPS/text/cover.xhtml", cover_xhtml, compress_type=zipfile.ZIP_DEFLATED)
            ez.writestr(f"OEBPS/images/cover.{cover_ext}", cover_bytes, compress_type=zipfile.ZIP_STORED)
        for fid, ch_title, body_html, _ in chapter_docs:
            ez.writestr(f"OEBPS/text/{fid}.xhtml", wrap_xhtml(ch_title, body_html), compress_type=zipfile.ZIP_DEFLATED)
        for im in sorted(used_images):
            data, _ = media_bytes[im]
            ez.writestr(f"OEBPS/images/{im}", data, compress_type=zipfile.ZIP_STORED)

    os.replace(tmp_epub, output_epub)
    return {
        "output": str(output_epub),
        "title": book_title,
        "author": book_author,
        "publisher": book_publisher,
        "isbn": book_isbn,
        "sections": len(chapter_docs),
        "total_chars": sum(s["chars"] for _, _, _, s in chapter_docs),
        "embedded_images": len(used_images),
        "media_stats": img_stats,
        "has_cover": cover_bytes is not None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pure-CPU fast path: inspect or convert an OCR-generated .docx into EPUB 3.3."
    )
    parser.add_argument("input_docx", type=pathlib.Path, help="Input OCR .docx file")
    parser.add_argument("output_epub", type=pathlib.Path, nargs="?", help="Output .epub path")
    parser.add_argument("--inspect", action="store_true", help="Read-only preflight inspection (do not build EPUB)")
    parser.add_argument("--cover-pdf", type=pathlib.Path, help="Optional source PDF to render page 1 as cover")
    parser.add_argument("--cover-image", type=pathlib.Path, help="Optional cover image file (JPEG/PNG)")
    parser.add_argument("--title", help="Override book title")
    parser.add_argument("--author", help="Override book author")
    parser.add_argument("--publisher", help="Override book publisher")
    parser.add_argument("--date", help="Override publication date (e.g. 2012-09)")
    parser.add_argument("--isbn", help="Override ISBN")
    parser.add_argument(
        "--chapters",
        type=pathlib.Path,
        help="Optional JSON file specifying chapters ([{start, id, title}]) and optional skip_ranges",
    )
    parser.add_argument("--min-img-bytes", type=int, default=4000, help="Drop media smaller than N bytes (default: 4000)")
    parser.add_argument("--min-img-dim", type=int, default=60, help="Drop media with min(w,h) < N px (default: 60)")
    args = parser.parse_args(argv)

    if args.inspect:
        report = inspect_docx(args.input_docx, min_bytes=args.min_img_bytes, min_dim=args.min_img_dim)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.output_epub is None:
        parser.error("output_epub is required unless --inspect is specified")

    chapters_spec = None
    skip_ranges: list[tuple[int, int]] = []
    if args.chapters is not None:
        raw_spec = json.loads(args.chapters.read_text(encoding="utf-8"))
        if isinstance(raw_spec, list):
            chapters_spec = raw_spec
        else:
            chapters_spec = raw_spec.get("chapters")
            skip_ranges = [tuple(pair) for pair in raw_spec.get("skip_ranges", [])]

    result = convert_docx_to_epub(
        args.input_docx,
        args.output_epub,
        cover_pdf=args.cover_pdf,
        cover_image=args.cover_image,
        title=args.title,
        author=args.author,
        publisher=args.publisher,
        date=args.date,
        isbn=args.isbn,
        chapters_spec=chapters_spec,
        skip_ranges=skip_ranges,
        min_img_bytes=args.min_img_bytes,
        min_img_dim=args.min_img_dim,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
