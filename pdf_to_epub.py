#!/usr/bin/env python3
"""
Convert a dual-layer OCR PDF (image + invisible text layer) into a clean,
reflowable EPUB 3 (with EPUB 2 NCX fallback) for WeChat Read and other readers.

Features:
- Extracts PDF page 1 as EPUB cover image (EPUB 3 + EPUB 2 WeChat Read compatible)
- Uses `pdftotext -bbox-layout` for accurate line coordinates
- Groups same-baseline fragments left-to-right to avoid out-of-order text
- Strips running headers (书名/篇名页眉) and footers (页码)
- Merges wrapped lines into natural Chinese paragraphs (including cross-page)
- Supports `--mineru-json` (MinerU 4.0 middle_json) to replace table pages with
  clean, sanitized, searchable HTML <table>s; falls back to crisp 1-bit PNG
  table images if no MinerU JSON is supplied
- Preserves chapter TOC from PDF bookmarks (Outlines)
- Zero external Python dependencies (uses stdlib + poppler `pdftotext`/`pdftoppm`)
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import html
from html.parser import HTMLParser
import json
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path


# -----------------------------
# 1. PDF Outline & Page-Rotation Parser
# -----------------------------
def _decompress_stream(obj_bytes: bytes) -> bytes | None:
    m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", obj_bytes, re.S)
    if not m:
        return None
    raw = m.group(1)
    if b"/FlateDecode" in obj_bytes:
        try:
            return zlib.decompress(raw)
        except Exception:
            return None
    return raw


def _decode_pdf_title(obj_bytes: bytes) -> str:
    m = re.search(rb"/Title\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f]+>)", obj_bytes)
    if not m:
        return ""
    raw = m.group(1)
    if raw.startswith(b"<"):
        try:
            hx = bytes.fromhex(raw[1:-1].decode("ascii"))
        except Exception:
            return ""
        if hx.startswith(b"\xfe\xff"):
            return hx[2:].decode("utf-16-be", "replace").strip()
        return hx.decode("utf-8", "replace").strip()
    inner = re.sub(rb"\\([()\\])", rb"\1", raw[1:-1])
    if inner.startswith(b"\xfe\xff"):
        return inner[2:].decode("utf-16-be", "replace").strip()
    return inner.decode("utf-8", "replace").strip()


def extract_pdf_metadata_and_outlines(pdf_path: str) -> tuple[list[tuple[int, str, int]], set[int]]:
    """Return (outlines, rotated_pages) where outlines is [(depth, title, 1-based page_num)]."""
    with open(pdf_path, "rb") as f:
        data = f.read()

    objs: dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj\b", data, re.S):
        objs[int(m.group(1))] = m.group(2)

    for k, v in list(objs.items()):
        if b"/ObjStm" in v:
            first_m = re.search(rb"/First\s+(\d+)", v)
            n_m = re.search(rb"/N\s+(\d+)", v)
            st = _decompress_stream(v)
            if first_m and n_m and st:
                first = int(first_m.group(1))
                n = int(n_m.group(1))
                header = st[:first].decode("latin1").split()
                pairs = [(int(header[2 * i]), int(header[2 * i + 1])) for i in range(n)]
                body = st[first:]
                for idx, (onum, off) in enumerate(pairs):
                    end = pairs[idx + 1][1] if idx + 1 < len(pairs) else len(body)
                    objs[onum] = body[off:end]

    def get_pages(ref: int) -> list[int]:
        body = objs.get(ref, b"")
        if re.search(rb"/Type\s*/Pages\b", body):
            kids_m = re.search(rb"/Kids\s*\[([^\]]+)\]", body)
            if not kids_m:
                return []
            res = []
            for k in re.findall(rb"(\d+)\s+0\s+R", kids_m.group(1)):
                res.extend(get_pages(int(k)))
            return res
        return [ref]

    root_m = re.search(rb"/Root\s+(\d+)\s+0\s+R", data)
    if not root_m:
        return [], set()
    root_body = objs.get(int(root_m.group(1)), b"")
    pages_m = re.search(rb"/Pages\s+(\d+)\s+0\s+R", root_body)
    if not pages_m:
        return [], set()

    ordered_pages = get_pages(int(pages_m.group(1)))
    page_to_num = {ref: i + 1 for i, ref in enumerate(ordered_pages)}

    rotated_pages: set[int] = set()
    for i, pref in enumerate(ordered_pages, 1):
        b = objs.get(pref, b"")
        rm = re.search(rb"/Rotate\s+(-?\d+)", b)
        if rm and int(rm.group(1)) % 180 != 0:
            rotated_pages.add(i)

    items: list[tuple[int, str, int]] = []
    outlines_m = re.search(rb"/Outlines\s+(\d+)\s+0\s+R", root_body)
    if outlines_m:
        def walk(ref: int | None, depth: int = 0):
            visited = set()
            while ref and ref not in visited:
                visited.add(ref)
                b = objs.get(ref, b"")
                title = _decode_pdf_title(b)
                dm = re.search(rb"/(?:D|Dest)\s*\[\s*(\d+)\s+0\s+R", b)
                pno = page_to_num.get(int(dm.group(1)), -1) if dm else -1
                if title and pno > 0:
                    items.append((depth, title, pno))
                fm = re.search(rb"/First\s+(\d+)\s+0\s+R", b)
                if fm:
                    walk(int(fm.group(1)), depth + 1)
                nm = re.search(rb"/Next\s+(\d+)\s+0\s+R", b)
                ref = int(nm.group(1)) if nm else None

        outlines_body = objs.get(int(outlines_m.group(1)), b"")
        first_m = re.search(rb"/First\s+(\d+)\s+0\s+R", outlines_body)
        if first_m:
            walk(int(first_m.group(1)), 0)

    return items, rotated_pages


# -----------------------------
# 2. MinerU 4.0 middle_json Parser & XHTML Table Sanitizer
# -----------------------------
ALLOWED_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td"}


class _XHTMLTableSanitizer(HTMLParser):
    """Parse MinerU HTML table and emit strictly well-formed XHTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        t = tag.lower()
        if t == "br":
            self.out.append("<br/>")
            return
        if t not in ALLOWED_TABLE_TAGS:
            return
        # Auto-close unclosed sibling td/th/tr
        if t in ("td", "th") and self.stack and self.stack[-1] in ("td", "th"):
            self.out.append(f"</{self.stack.pop()}>")
        elif t == "tr":
            while self.stack and self.stack[-1] in ("td", "th", "tr"):
                self.out.append(f"</{self.stack.pop()}>")

        clean_attrs = []
        for k, v in attrs:
            kl = k.lower()
            if kl in ("rowspan", "colspan") and v and v.strip().isdigit():
                val = int(v.strip())
                if val > 1:
                    clean_attrs.append(f' {kl}="{val}"')
        self.out.append(f"<{t}{''.join(clean_attrs)}>")
        self.stack.append(t)

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t not in ALLOWED_TABLE_TAGS:
            return
        if t in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == t:
                    break

    def handle_data(self, data: str):
        if data:
            self.out.append(html.escape(data))

    def get_xhtml(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_table_html(raw_html: str) -> str:
    parser = _XHTMLTableSanitizer()
    parser.feed(raw_html)
    parser.close()
    res = parser.get_xhtml().strip()
    if not res.startswith("<table"):
        return f"<p>{html.escape(raw_html)}</p>"
    # Final XML well-formedness check
    try:
        ET.fromstring(res)
        return res
    except ET.ParseError:
        return f"<p>{html.escape(re.sub(r'<[^>]+>', ' ', raw_html))}</p>"


def _extract_spans_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                c = item.get("content", "")
                if isinstance(c, str):
                    parts.append(c)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return ""


MINERU_HEADER_RE = re.compile(
    r"^(?:双\s*城.{0,2}志|第[一二三四五六七八九十百]+[篇编]\s*.{0,15})$"
)
MINERU_REPEAT_RE = re.compile(r"(.{3,20}?)\1{4,}")


def _is_hallucinated_table(raw_html: str) -> bool:
    """Detect VLM repetition loops / abnormally bloated cells in MinerU tables."""
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", raw_html, re.S)
    if not cells:
        return True
    max_cell = max(len(c) for c in cells)
    if max_cell > 300 or len(raw_html) > 12000:
        return True
    for c in cells:
        if len(c) > 40 and MINERU_REPEAT_RE.search(c):
            return True
    return False


def load_mineru_pages(mineru_json_path: str) -> tuple[dict[int, list[tuple[str, str]]], set[int]]:
    """
    Parse MinerU 4.0 middle_json into:
      (good_pages_dict, fallback_image_pages_set)
    good_pages_dict maps 1-based page_num -> [(kind, content), ...].
    Pages with VLM hallucination/loop tables are placed in fallback_image_pages_set.
    """
    with open(mineru_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result: dict[int, list[tuple[str, str]]] = {}
    fallback_pages: set[int] = set()
    for pg in data.get("pages", []):
        pno = int(pg["page_idx"]) + 1
        blocks: list[tuple[str, str]] = []
        page_has_bad_table = False
        for b in pg.get("blocks", []):
            btype = b.get("type", "")
            if btype in ("header", "footer", "page_number", "aside_text"):
                continue
            if btype == "paragraph_title":
                txt = _extract_spans_text(b.get("content"))
                if txt and not MINERU_HEADER_RE.match(txt):
                    blocks.append(("h3", txt))
            elif btype == "text":
                txt = _extract_spans_text(b.get("content"))
                if txt and not MINERU_HEADER_RE.match(txt):
                    blocks.append(("p", txt))
            elif btype == "table":
                for sub in b.get("content", []):
                    if not isinstance(sub, dict):
                        continue
                    st = sub.get("type", "")
                    sc = sub.get("content", "")
                    if st == "table_caption":
                        txt = _extract_spans_text(sc)
                        if txt and not MINERU_HEADER_RE.match(txt):
                            blocks.append(("table_caption", txt))
                    elif st == "table_body":
                        raw_html = sc if isinstance(sc, str) else _extract_spans_text(sc)
                        if raw_html:
                            if _is_hallucinated_table(raw_html):
                                page_has_bad_table = True
                                break
                            blocks.append(("raw_table", sanitize_table_html(raw_html)))
                    elif st == "table_footnote":
                        txt = _extract_spans_text(sc)
                        if txt and not MINERU_HEADER_RE.match(txt):
                            blocks.append(("table_footnote", txt))
                if page_has_bad_table:
                    break
        if page_has_bad_table:
            fallback_pages.add(pno)
        elif blocks:
            result[pno] = blocks
    return result, fallback_pages


# -----------------------------
# 3. Layout-Aware Text Extraction, Table Detection & Paragraph Merging
# -----------------------------
HEADER_RE = re.compile(
    r"^(?:双\s*城\s*县\s*志|第[一二三四五六七八九十百]+[篇编].{0,15})$"
)
FOOTER_RE = re.compile(r"^[·•.\-:：—_~`'\"*\s\d]+$")
END_PUNCT = set("。！？：；”）》】…!?")
HEADING_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百千]+[篇编章节]\s*.*|序\s*言|凡\s*例|概\s*述|大\s*事\s*记|附\s*录|后\s*记|编后记|续\s*表|表[一二三四五六七八九十百\d]+.*)$"
)
YEAR_HEADING_RE = re.compile(r"^\d{4}年（[^）]{1,20}）$")
TABLE_LINE_RE = re.compile(
    r"^(?:续\s*表.*|表\s*[一二三四五六七八九十百\d]+.{0,24}|单位\s*[:：].{0,18}|.{2,26}(?:统计表|一览表|简表|分类表|名录表|情况表|汇总表|调查表|分布表|对照表|明细表|登记表|年表))$"
)


def clean_ocr_text(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.strip()


def extract_pages_layout(pdf_path: str) -> list[dict]:
    """Run `pdftotext -bbox-layout` once and parse all pages."""
    out = subprocess.check_output(
        ["pdftotext", "-bbox-layout", pdf_path, "-"],
        text=True,
        errors="replace",
    )
    m = re.search(r"<doc>.*</doc>", out, re.S)
    if not m:
        raise RuntimeError("Failed to parse pdftotext -bbox-layout output")
    root = ET.fromstring(m.group(0))

    pages = []
    for p_idx, page_el in enumerate(root.findall("page"), 1):
        pw = float(page_el.attrib.get("width", "502"))
        ph = float(page_el.attrib.get("height", "733"))
        raw_lines = []
        for line_el in page_el.iter("line"):
            words = [w.text or "" for w in line_el.findall("word")]
            txt = clean_ocr_text("".join(words))
            if not txt:
                continue
            x0 = float(line_el.attrib["xMin"])
            y0 = float(line_el.attrib["yMin"])
            x1 = float(line_el.attrib["xMax"])
            y1 = float(line_el.attrib["yMax"])
            raw_lines.append((x0, y0, x1, y1, txt))

        raw_lines.sort(key=lambda r: ((r[1] + r[3]) / 2.0, r[0]))
        merged_lines = []
        for r in raw_lines:
            cy = (r[1] + r[3]) / 2.0
            if merged_lines:
                prev = merged_lines[-1]
                pcy = (prev[1] + prev[3]) / 2.0
                if abs(cy - pcy) <= 6.0:
                    prev[5].append(r)
                    prev[0] = min(prev[0], r[0])
                    prev[1] = min(prev[1], r[1])
                    prev[2] = max(prev[2], r[2])
                    prev[3] = max(prev[3], r[3])
                    continue
            merged_lines.append([r[0], r[1], r[2], r[3], "", [r]])

        visual_lines = []
        for ml in merged_lines:
            frags = sorted(ml[5], key=lambda f: f[0])
            parts = [frags[0][4]]
            for prev_f, cur_f in zip(frags, frags[1:]):
                gap = cur_f[0] - prev_f[2]
                if gap > 14.0:
                    parts.append("  " + cur_f[4])
                else:
                    parts.append(cur_f[4])
            full_txt = "".join(parts).strip()
            visual_lines.append((ml[0], ml[1], ml[2], ml[3], full_txt))

        pages.append({
            "page": p_idx,
            "width": pw,
            "height": ph,
            "lines": visual_lines,
        })
    return pages


def detect_table_pages(
    pages: list[dict],
    rotated_pages: set[int],
    min_body_page: int,
) -> set[int]:
    """Identify pages that are tables."""
    table_pages = set(rotated_pages)
    for p in pages:
        pno = p["page"]
        if pno < min_body_page or pno in table_pages:
            continue
        lines = p["lines"]
        if not lines:
            continue
        full_text = "\n".join(l[4] for l in lines)
        period_count = full_text.count("。")
        has_table_header = any(
            len(l[4]) <= 34 and TABLE_LINE_RE.match(l[4])
            for l in lines
        )
        if has_table_header and period_count <= 8:
            table_pages.add(pno)
    return table_pages


def render_cover_image(pdf_path: str, tmp_dir: str, dpi: int = 150) -> bytes | None:
    prefix = os.path.join(tmp_dir, "cover")
    subprocess.run(
        ["pdftoppm", "-jpeg", "-r", str(dpi), "-f", "1", "-l", "1", pdf_path, prefix],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    matches = sorted(Path(tmp_dir).glob("cover*.jpg"))
    return matches[0].read_bytes() if matches else None


def render_table_pages(pdf_path: str, table_pages: set[int], tmp_dir: str, dpi: int = 150) -> dict[int, bytes]:
    if not table_pages:
        return {}

    def _render_one(pno: int) -> tuple[int, bytes]:
        prefix = os.path.join(tmp_dir, f"tbl_{pno:04d}")
        subprocess.run(
            ["pdftoppm", "-png", "-mono", "-r", str(dpi), "-f", str(pno), "-l", str(pno), pdf_path, prefix],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        matches = sorted(Path(tmp_dir).glob(f"tbl_{pno:04d}*.png"))
        return pno, matches[0].read_bytes()

    results: dict[int, bytes] = {}
    workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for pno, img_bytes in ex.map(_render_one, sorted(table_pages)):
            results[pno] = img_bytes
    return results


def filter_and_merge_paragraphs(
    pages_slice: list[dict],
    table_image_pages: set[int],
    mineru_pages: dict[int, list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """
    Convert a list of page dicts into a list of (kind, content) blocks:
    kind in {"h2", "h3", "p", "img", "table_caption", "table_footnote", "raw_table"}.
    """
    blocks: list[tuple[str, str]] = []
    cur_para = ""
    prev_ended_short = False

    def flush_para():
        nonlocal cur_para
        if cur_para:
            blocks.append(("p", cur_para))
            cur_para = ""

    for p in pages_slice:
        pno = p["page"]
        if pno in mineru_pages:
            flush_para()
            blocks.extend(mineru_pages[pno])
            prev_ended_short = True
            continue
        if pno in table_image_pages:
            flush_para()
            blocks.append(("img", f"images/p{pno:04d}.png"))
            prev_ended_short = True
            continue

        ph = p["height"]
        lines = p["lines"]
        if not lines:
            continue

        body_lines = []
        for x0, y0, x1, y1, txt in lines:
            lh = y1 - y0
            if y0 < ph * 0.16 and lh < 24.0 and len(txt) <= 20 and HEADER_RE.match(txt):
                continue
            if y0 > ph * 0.88 and len(txt) <= 15 and FOOTER_RE.match(txt):
                continue
            if y0 > ph * 0.90 and len(txt) <= 4 and re.match(r"^[\d.:·•\-]+$", txt):
                continue
            body_lines.append((x0, y0, x1, y1, txt))

        if not body_lines:
            continue

        long_lines = [l for l in body_lines if len(l[4]) >= 18]
        if long_lines:
            xs = sorted(l[0] for l in long_lines)
            left_margin = xs[max(0, int(len(xs) * 0.15))]
            x1s = sorted(l[2] for l in long_lines)
            right_margin = x1s[min(len(x1s) - 1, int(len(x1s) * 0.85))]
        else:
            left_margin = min(l[0] for l in body_lines)
            right_margin = max(l[2] for l in body_lines)

        for idx, (x0, y0, x1, y1, txt) in enumerate(body_lines):
            lh = y1 - y0
            indent = x0 - left_margin
            is_centered = (x0 > left_margin + 35) and (x1 < right_margin - 25) and len(txt) <= 32
            is_short_heading = (
                len(txt) <= 36
                and (HEADING_RE.match(txt) or YEAR_HEADING_RE.match(txt))
                and (is_centered or lh >= 16.5 or indent >= 25)
            )

            if is_short_heading:
                flush_para()
                if re.match(r"^第[一二三四五六七八九十百]+[篇编]", txt):
                    blocks.append(("h2", txt))
                else:
                    blocks.append(("h3", txt))
                prev_ended_short = True
                continue

            starts_new_para = False
            if not cur_para:
                starts_new_para = True
            elif 13.0 <= indent <= 42.0:
                starts_new_para = True
            elif prev_ended_short and cur_para[-1] in END_PUNCT:
                starts_new_para = True
            elif idx > 0 and (y0 - body_lines[idx - 1][3]) > max(16.0, lh * 1.4):
                starts_new_para = True

            if starts_new_para:
                flush_para()
                cur_para = txt
            else:
                if (
                    cur_para
                    and cur_para[-1].isascii()
                    and cur_para[-1].isalnum()
                    and txt
                    and txt[0].isascii()
                    and txt[0].isalnum()
                ):
                    cur_para += " " + txt
                else:
                    cur_para += txt

            prev_ended_short = (right_margin - x1) > 36.0 and len(txt) < 35

    flush_para()
    return blocks


# -----------------------------
# 4. EPUB 3 + NCX Builder
# -----------------------------
CSS = """body {
  font-family: "Songti SC", "STSong", "Noto Serif CJK SC", "SimSun", serif;
  line-height: 1.75;
  margin: 0 0.8em;
  color: #222;
}
h1 {
  font-size: 1.45em;
  text-align: center;
  margin: 1.4em 0 1em;
  line-height: 1.4;
}
h2 {
  font-size: 1.25em;
  text-align: center;
  margin: 1.3em 0 0.8em;
  line-height: 1.4;
}
h3 {
  font-size: 1.08em;
  font-weight: bold;
  margin: 1.1em 0 0.5em;
  line-height: 1.4;
}
p {
  text-indent: 2em;
  margin: 0.45em 0;
  text-align: justify;
}
p.table-caption {
  text-indent: 0;
  text-align: center;
  font-weight: bold;
  margin: 1em 0 0.35em;
}
p.table-footnote {
  text-indent: 0;
  font-size: 0.85em;
  color: #555;
  margin: 0.25em 0 0.8em;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.5em 0 0.9em;
  font-size: 0.82em;
  line-height: 1.45;
}
th, td {
  border: 1px solid #999;
  padding: 0.25em 0.35em;
  text-align: left;
  vertical-align: top;
  word-break: break-all;
}
figure.table-page {
  margin: 0.8em 0;
  padding: 0;
  text-align: center;
}
figure.table-page img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0 auto;
}
div.cover-wrap {
  text-align: center;
  margin: 0;
  padding: 0;
}
div.cover-wrap img {
  max-width: 100%;
  height: auto;
}
"""


def build_epub(
    title: str,
    author: str,
    chapters: list[dict],
    out_path: str,
    cover_bytes: bytes | None = None,
    table_images: dict[int, bytes] | None = None,
):
    book_id = f"urn:uuid:{uuid.uuid4()}"
    table_images = table_images or {}

    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
        zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)

        manifest_items = [
            '    <item id="css" href="style.css" media-type="text/css"/>',
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        ]
        spine_items = []
        nav_lis = []
        ncx_points = []
        meta_extra = ""
        play_order = 1

        if cover_bytes:
            zf.writestr("OEBPS/cover.jpg", cover_bytes, compress_type=zipfile.ZIP_STORED)
            cover_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>封面</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <div class="cover-wrap"><img src="cover.jpg" alt="{html.escape(title)}封面"/></div>
</body>
</html>
"""
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml, compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append(
                '    <item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
            )
            manifest_items.append(
                '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            )
            spine_items.append('    <itemref idref="cover" linear="yes"/>')
            meta_extra = '\n    <meta name="cover" content="cover-image"/>'

        for pno, img_bytes in sorted(table_images.items()):
            rel_href = f"images/p{pno:04d}.png"
            zf.writestr(f"OEBPS/{rel_href}", img_bytes, compress_type=zipfile.ZIP_STORED)
            manifest_items.append(
                f'    <item id="img-p{pno:04d}" href="{rel_href}" media-type="image/png"/>'
            )

        for ch in chapters:
            cid = ch["id"]
            fname = ch["filename"]
            ctitle = ch["title"]

            body_parts = [f'  <h1 id="top">{html.escape(ctitle)}</h1>']
            for kind, content in ch["blocks"]:
                if kind == "raw_table":
                    body_parts.append(f"  {content}")
                elif kind == "table_caption":
                    body_parts.append(f'  <p class="table-caption">{html.escape(content)}</p>')
                elif kind == "table_footnote":
                    body_parts.append(f'  <p class="table-footnote">{html.escape(content)}</p>')
                elif kind == "img":
                    body_parts.append(
                        f'  <div class="table-page"><img src="{content}" alt="原书表格"/></div>'
                    )
                elif kind == "h2":
                    body_parts.append(f"  <h2>{html.escape(content)}</h2>")
                elif kind == "h3":
                    body_parts.append(f"  <h3>{html.escape(content)}</h3>")
                else:
                    body_parts.append(f"  <p>{html.escape(content)}</p>")

            xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>{html.escape(ctitle)}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
{chr(10).join(body_parts)}
</body>
</html>
"""
            zf.writestr(f"OEBPS/{fname}", xhtml, compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append(
                f'    <item id="{cid}" href="{fname}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'    <itemref idref="{cid}"/>')
            nav_lis.append(f'      <li><a href="{fname}">{html.escape(ctitle)}</a></li>')
            ncx_points.append(
                f"""    <navPoint id="nav-{play_order}" playOrder="{play_order}">
      <navLabel><text>{html.escape(ctitle)}</text></navLabel>
      <content src="{fname}"/>
    </navPoint>"""
            )
            play_order += 1

        nav_xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <title>目录</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>目录</h1>
    <ol>
{chr(10).join(nav_lis)}
    </ol>
  </nav>
</body>
</html>
"""
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)

        toc_ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>
{chr(10).join(ncx_points)}
  </navMap>
</ncx>
"""
        zf.writestr("OEBPS/toc.ncx", toc_ncx, compress_type=zipfile.ZIP_DEFLATED)

        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookId" xml:lang="zh-CN">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">2026-11-26T00:00:00Z</meta>{meta_extra}
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine_items)}
  </spine>
</package>
"""
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)


def split_large_chapter_blocks(
    ctitle: str,
    blocks: list[tuple[str, str]],
    max_bytes: int = 50_000,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Split oversized chapters at safe block boundaries (never breaking caption+table)."""
    est_total = sum(len(c.encode("utf-8")) + 20 for _, c in blocks)
    if est_total <= max_bytes:
        return [(ctitle, blocks)]

    chunks: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_bytes = 0
    i = 0
    while i < len(blocks):
        # Keep table_caption paired with its immediately following table
        group = [blocks[i]]
        if blocks[i][0] in ("table_caption", "h3") and i + 1 < len(blocks) and blocks[i + 1][0] == "raw_table":
            group.append(blocks[i + 1])
            i += 2
        else:
            i += 1
        g_bytes = sum(len(c.encode("utf-8")) + 20 for _, c in group)
        if cur and cur_bytes + g_bytes > max_bytes:
            chunks.append(cur)
            cur = list(group)
            cur_bytes = g_bytes
        else:
            cur.extend(group)
            cur_bytes += g_bytes
    if cur:
        chunks.append(cur)

    if len(chunks) == 1:
        return [(ctitle, chunks[0])]
    return [(f"{ctitle}（{idx + 1}）", ch) for idx, ch in enumerate(chunks)]


def main():
    parser = argparse.ArgumentParser(description="Convert dual-layer OCR PDF to clean reflowable EPUB.")
    parser.add_argument("pdf", help="Input dual-layer PDF path")
    parser.add_argument("-o", "--output", help="Output EPUB path")
    parser.add_argument("--title", default=None, help="Book title")
    parser.add_argument("--author", default="地方志编纂委员会", help="Book author")
    parser.add_argument("--mineru-json", default=None, help="MinerU 4.0 middle_json for HTML table pages")
    parser.add_argument("--no-cover", action="store_true", help="Do not embed page 1 as cover")
    parser.add_argument("--no-table-images", action="store_true", help="Do not render table pages as images")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for cover & fallback table images (default: 150)")
    args = parser.parse_args()

    pdf_path = os.path.abspath(args.pdf)
    out_path = args.output or str(Path(pdf_path).with_name(
        re.sub(r"_双层$", "", Path(pdf_path).stem) + ".epub"
    ))
    title = args.title or re.sub(r"^[A-Z0-9]+\.", "", re.sub(r"_双层$", "", Path(pdf_path).stem))

    print(f"[1/5] Parsing PDF outlines & page rotation: {pdf_path}")
    outlines, rotated_pages = extract_pdf_metadata_and_outlines(pdf_path)
    print(f"      Found {len(outlines)} outline entries, {len(rotated_pages)} rotated landscape pages")

    print("[2/5] Extracting layout-aware text via pdftotext -bbox-layout ...")
    pages = extract_pages_layout(pdf_path)
    total_pages = len(pages)
    print(f"      Extracted {total_pages} pages")

    toc_pages = {p for d, t, p in outlines if d == 0 and t == "目录"}
    min_body_page = min((p for d, t, p in outlines if d == 0 and t not in ("序言", "凡例", "目录")), default=1)
    toc_range = (min(toc_pages), min_body_page - 1) if toc_pages and min_body_page > min(toc_pages) else None

    split_points: list[tuple[int, str]] = []
    seen_pages = set()
    for d, t, p in outlines:
        if d > 1 or t == "目录":
            continue
        if toc_range and toc_range[0] <= p <= toc_range[1]:
            continue
        if p in seen_pages:
            continue
        seen_pages.add(p)
        split_points.append((p, t))
    split_points.sort(key=lambda x: x[0])

    if not split_points:
        step = 20
        split_points = [(p, f"第 {p}–{min(p+step-1, total_pages)} 页") for p in range(1, total_pages + 1, step)]

    mineru_pages: dict[int, list[tuple[str, str]]] = {}
    mineru_fallback_pages: set[int] = set()
    if args.mineru_json:
        mineru_pages, mineru_fallback_pages = load_mineru_pages(args.mineru_json)
        print(
            f"[3/5] Loaded MinerU HTML tables for {len(mineru_pages)} pages "
            f"({len(mineru_fallback_pages)} hallucinated/looped pages -> image fallback)"
        )

    table_image_pages: set[int] = set(mineru_fallback_pages)
    if not args.no_table_images:
        detected = detect_table_pages(pages, rotated_pages, min_body_page)
        table_image_pages |= (detected - set(mineru_pages.keys()))
        if table_image_pages:
            print(f"      Rendering {len(table_image_pages)} fallback table pages as images: {sorted(table_image_pages)}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cover_bytes = None if args.no_cover else render_cover_image(pdf_path, tmp_dir, dpi=args.dpi)
        table_images = render_table_pages(pdf_path, table_image_pages, tmp_dir, dpi=args.dpi)

    print(f"[4/5] Building {len(split_points)} chapters & merging paragraphs/tables ...")
    chapters = []
    used_table_image_pages: set[int] = set()
    total_blocks = 0
    total_html_tables = 0
    for idx, (start_p, ctitle) in enumerate(split_points):
        end_p = split_points[idx + 1][0] - 1 if idx + 1 < len(split_points) else total_pages
        page_slice = [
            pages[p - 1]
            for p in range(start_p, end_p + 1)
            if not (toc_range and toc_range[0] <= p <= toc_range[1])
        ]
        for p in page_slice:
            if p["page"] in table_image_pages:
                used_table_image_pages.add(p["page"])
        blocks = filter_and_merge_paragraphs(page_slice, table_image_pages, mineru_pages)
        if not blocks:
            continue
        for sub_title, sub_blocks in split_large_chapter_blocks(ctitle, blocks, max_bytes=50_000):
            total_blocks += len(sub_blocks)
            total_html_tables += sum(1 for k, _ in sub_blocks if k == "raw_table")
            chapters.append({
                "id": f"ch{len(chapters) + 1:03d}",
                "filename": f"ch{len(chapters) + 1:03d}.xhtml",
                "title": sub_title,
                "blocks": sub_blocks,
            })

    table_images = {p: b for p, b in table_images.items() if p in used_table_image_pages}

    print(f"[5/5] Writing EPUB: {out_path}")
    build_epub(
        title=title,
        author=args.author,
        chapters=chapters,
        out_path=out_path,
        cover_bytes=cover_bytes,
        table_images=table_images,
    )
    size_kb = os.path.getsize(out_path) / 1024.0
    print(
        f"Done! {len(chapters)} chapters, {total_blocks} blocks "
        f"({total_html_tables} HTML tables, {len(table_images)} image tables, cover={'yes' if cover_bytes else 'no'}), "
        f"{size_kb:.1f} KB"
    )


if __name__ == "__main__":
    main()
