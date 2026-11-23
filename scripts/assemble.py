"""Assemble final deliverables from a tool's normalized doc.json.

  python assemble.py <doc.json> <source.pdf> <out_dir> [--title 西岗区志] [--no-recrop]

Writes into <out_dir>:
  images/            figure + table crops re-cut from the embedded scan at native resolution
  book.md            Markdown (headings 编/章/节..., paragraphs joined across columns/pages,
                     tables = screenshot + HTML table, figures referenced in place)
  book.json          doc.json enriched: final heading levels, image paths, join flags
  book.epub          pandoc EPUB 3 (TOC from headings) -- epubcheck is run separately
"""
import io
import re
import sys
import json
import html
import shutil
import zipfile
import argparse
import pathlib
import subprocess
import unicodedata
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from lxml import etree, html as lhtml
from recrop import NativePage
from textlayer import clean_html_equations

CN = "一二三四五六七八九十百零〇"
GAZETTEER_RX = re.compile(rf"^第[{CN}\d]+[编章节]")
HEAD_RULES = [
    (re.compile(rf"^第[{CN}\d]+编"), 1),
    (re.compile(r"^(序|序言|凡例|概述|总述|大事记|附录|后记|编后记|索引|目录|人物|专记)$"), 1),
    (re.compile(rf"^第[{CN}\d]+章"), 2),
    (re.compile(rf"^第[{CN}\d]+节"), 3),
    (re.compile(rf"^[{CN}]+、"), 4),
    (re.compile(rf"^[（(][{CN}]+[）)]"), 5),
]
GENERAL_FRONT_BACK_RX = re.compile(
    r"^(序|序言|自序|再版序|译者序|编者序|编者序言|前言|导言|引论|绪论|总论|目录|附录|后记|编后记|译后记|结语|尾声|参考文献|索引|跋)$"
    r"|^附录[{CN}\d：:·\s]".replace("{CN}", CN)
)
GENERAL_HEAD_RULES = [
    (re.compile(rf"^第[{CN}\d]+[部篇卷辑]"), 1),
    (re.compile(rf"^第[{CN}\d]+[章回]"), 2),
    (re.compile(rf"^第[{CN}\d]+节"), 3),
    (re.compile(r"^\d{1,3}[.．、]\s*\S"), 3),
    (re.compile(rf"^[{CN}]+、"), 4),
    (re.compile(rf"^[（(][{CN}\d]+[）)]"), 5),
]
SENT_END = tuple("。！？；：」』”）)!?;:…")
SKIP = {"header", "footer", "page_number", "aside"}


def _norm_heading(s):
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\W_]+", "", s)


def heading_level(text, tool_level):
    t = re.sub(r"\s+", "", text)
    for rx, lvl in HEAD_RULES:
        if rx.search(t):
            return lvl
    return max(4, min(6, (tool_level or 4) + 3)) if tool_level else 4


def is_gazetteer_book(doc):
    """Detect whether the book follows the 地方志 编/章/节 hierarchy."""
    hits = 0
    for p in doc.get("pages", []):
        for b in p.get("blocks", []):
            if b.get("type") == "title":
                t = re.sub(r"\s+", "", b.get("text") or "")
                if GAZETTEER_RX.search(t):
                    hits += 1
                    if hits >= 3:
                        return True
    return False


def build_toc_matcher(pdf):
    """Build a per-page matcher from the source PDF's built-in outline (if >= 3 entries)."""
    toc = pdf.get_toc() or []
    if len(toc) < 3:
        return None
    from rapidfuzz import fuzz
    by_page = {}
    for lvl, title, pno in toc:
        nt = _norm_heading(title)
        if not nt:
            continue
        by_page.setdefault(pno, []).append((max(1, min(3, int(lvl))), nt))

    def match(pno, text):
        nt = _norm_heading(text)
        if not nt:
            return None
        best_lvl, best_score = None, 0.0
        for dp in (0, -1, 1):
            for lvl, cand in by_page.get(pno + dp, []):
                if nt == cand or (len(nt) >= 3 and len(cand) >= 3 and (nt in cand or cand in nt)):
                    score = 100.0 if dp == 0 else 92.0
                else:
                    score = fuzz.ratio(nt, cand) - (5.0 if dp != 0 else 0.0)
                if score > best_score:
                    best_score, best_lvl = score, lvl
        return best_lvl if best_score >= 72.0 else None

    return match


def general_heading_shift(doc):
    """For non-gazetteer books without a source PDF TOC, compute upward shift so top headings start at H1."""
    lvls = []
    for p in doc.get("pages", []):
        for b in p.get("blocks", []):
            if b.get("type") != "title":
                continue
            t = re.sub(r"\s+", "", b.get("text") or "")
            for rx, lvl in GENERAL_HEAD_RULES:
                if rx.search(t):
                    lvls.append(lvl)
                    break
    return (min(lvls) - 1) if lvls else 0


def general_heading_level(text, tool_level, shift=0):
    t = re.sub(r"\s+", "", text)
    if GENERAL_FRONT_BACK_RX.search(t):
        return 1
    for rx, lvl in GENERAL_HEAD_RULES:
        if rx.search(t):
            return max(1, min(5, lvl - shift))
    return max(4, min(6, (tool_level or 4) + 3)) if tool_level else 4


def clean_title(text):
    # Strip OCR spacing between CJK characters while preserving spaces inside English phrases
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+|\s+(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])", "", t)
    return re.sub(rf"^(第[{CN}\d]+[编章节])", r"\1 ", t)


ALLOWED_TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "col", "caption", "sup", "sub", "br", "b", "i", "strong", "em", "u", "s", "span"}


def sanitize_prose_md(s):
    """Escape raw <...> pseudo-tags (e.g. <中华人民共和国文物保护法>, <通告>) and accidental [text](target)
    false links (e.g. dialect IPA [kuə](1) or doc numbers [1986](78号)) in plain prose lines."""
    if not s:
        return s
    s = re.sub(r"<(/?)([^<>\s][^<>]{0,120})>",
               lambda m: m.group(0) if re.match(r"^(?:sup|sub|br\s*/?)$", m.group(2), re.I) else f"&lt;{m.group(1)}{m.group(2)}&gt;",
               s)
    s = re.sub(r"(?<!\!)\]\(", r"]\(", s)
    # Escape OCR-emitted `^[` (and unclosed `[` after `^`) to prevent exponential backtracking / OOM in Pandoc
    s = s.replace("^[", r"\^\[")
    return s


def xhtml_table(raw):
    """Normalize tool HTML table into well-formed XHTML (quoted attrs, closed tags)."""
    raw = clean_html_equations(raw)
    try:
        frag = lhtml.fragment_fromstring(raw, create_parent=False)
    except Exception:
        frag = lhtml.fragment_fromstring(raw, create_parent="div")
    tbl = frag if frag.tag == "table" else frag.find(".//table")
    if tbl is None:
        return None
    for img in list(tbl.findall(".//img")):
        if not (img.attrib.get("src") or "").strip():
            parent = img.getparent()
            if parent is not None:
                if img.tail:
                    prev = img.getprevious()
                    if prev is not None:
                        prev.tail = (prev.tail or "") + img.tail
                    else:
                        parent.text = (parent.text or "") + img.tail
                parent.remove(img)
    for el in list(tbl.iter()):
        if isinstance(el.tag, str) and el.tag.lower() not in ALLOWED_TABLE_TAGS:
            el.drop_tag()
            continue
        for k in list(el.attrib):
            if k not in ("colspan", "rowspan"):
                del el.attrib[k]
    for cell in tbl.iter("td", "th"):
        # Normalize whitespace while preserving safe inline tags (sup, sub, br) allowed by ALLOWED_TABLE_TAGS
        for child in list(cell):
            if not isinstance(child.tag, str) or child.tag.lower() not in ("sup", "sub", "br"):
                child.drop_tag()
        if cell.text:
            cell.text = re.sub(r"\s+", " ", cell.text)
        for child in cell:
            if child.text:
                child.text = re.sub(r"\s+", " ", child.text)
            if child.tail:
                child.tail = re.sub(r"\s+", " ", child.tail)
        if len(cell) == 0:
            cell.text = (cell.text or "").strip()
        else:
            if cell.text:
                cell.text = cell.text.lstrip()
            if cell[-1].tail:
                cell[-1].tail = cell[-1].tail.rstrip()
    return etree.tostring(tbl, method="xml", encoding="unicode")


def table_rows(xml):
    t = etree.fromstring(xml)
    return t, [tr for tr in t.iter("tr")]


def row_text(tr):
    return "|".join("".join(td.itertext()).strip() for td in tr)


def merge_continued_table(prev_xml, cont_xml):
    """Append rows of a 续表 to the previous table, dropping repeated header rows."""
    pt, prows = table_rows(prev_xml)
    ct, crows = table_rows(cont_xml)
    head = [row_text(r) for r in prows[:4]]
    k = 0
    while k < len(crows) and k < 4 and row_text(crows[k]) in head:
        k += 1
    body = pt.find("tbody") if pt.find("tbody") is not None else pt
    for r in crows[k:]:
        body.append(r)
    return etree.tostring(pt, method="xml", encoding="unicode"), k


def _make_note_icon_png_bytes():
    """Render a crisp 96x96 RGBA WeRead-style warm-orange circular '注' icon as PNG bytes."""
    S = 192
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    pad = 8
    draw.ellipse((pad, pad, S - pad, S - pad), fill=(228, 152, 68, 255))
    font = None
    for fp, idx in (
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
        ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 2),
    ):
        if pathlib.Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, int(S * 0.56), index=idx)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()
    ch = "注"
    bbox = draw.textbbox((0, 0), ch, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (S - tw) / 2 - bbox[0]
    ty = (S - th) / 2 - bbox[1]
    draw.text((tx, ty), ch, fill=(255, 255, 255, 255), font=font)
    im = im.resize((96, 96), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def patch_epub_postprocess(epub_path, isbn="", footnote_mode="dual"):
    """Post-process the Pandoc-built EPUB zip:
    1) Inject EPUB 2-compatible <meta name="cover" content="..."/> alongside EPUB 3's
       properties="cover-image" so readers like WeRead (微信读书) display the cover on the bookshelf.
    2) Optionally append <dc:identifier>urn:isbn:...</dc:identifier> when extracted from copyright page.
    3) Remove deprecated EPUB 2 <guide>...</guide> block and any <itemref idref="nav"/> in <spine>
       so WeRead does not render an inline TOC + bogus Guide/Pages dump inside the reading flow
       (sidebar TOC still comes cleanly from nav.xhtml + toc.ncx, matching 100% of commercial EPUBs).
    4) Strip synthetic `.page-break-only` <h1> split markers from front/back-matter XHTML files
       so each special page starts on its own file/page without showing a fake heading.
    5) Footnotes (`footnote_mode`):
       - `dual` (default): wrap WeRead's `<img alt="..." class="qqreader-footnote" src="../media/note.png"/>`
         INSIDE standard EPUB 3 `<a href="#fnN" class="footnote-ref duokan-footnote" id="fnrefN"
         epub:type="noteref" role="doc-noteref">...</a>` AND preserve `<aside epub:type="footnote">`
         with working backlinks -> full Apple Books / Kobo / Duokan pop-up & jump + WeRead icon/alt.
       - `weread`: naked inline `<img alt="..." class="qqreader-footnote" src="../media/note.png"/>`
         (no wrapping `<a>` so WeRead cannot prioritize anchor jump over `qqreader-footnote`), with
         plain-text chapter-end reference list.
       - `epub3`: pure superscript `<a epub:type="noteref"><sup>N</sup></a>` + `<aside epub:type="footnote">`.
    """
    epub_path = pathlib.Path(epub_path)
    tmp_path = epub_path.with_suffix(".epub.tmp")
    pb_h1_rx = re.compile(r'<h1\b[^>]*\bpage-break-only\b[^>]*>.*?</h1>\s*', re.S)
    fn_ref_rx = re.compile(
        r'(<a\b[^>]*class="footnote-ref)("[^>]*epub:type="noteref"[^>]*>)(\d+)(</a>)'
    )
    fn_ref_full_rx = re.compile(
        r'(<a\b[^>]*href="#fn(\d+)"[^>]*class="footnote-ref)("[^>]*>)(\d+)(</a>)'
    )
    fn_aside_rx = re.compile(
        r'<aside\b[^>]*\bid="fn(\d+)"[^>]*>(.*?)</aside>', re.S
    )
    fn_sec_block_rx = re.compile(
        r'<section\s+id="footnotes"\s+class="footnotes footnotes-end-of-document"\s+epub:type="footnotes">(.*?)</section>',
        re.S,
    )
    fn_back_rx = re.compile(
        r'<a\b[^>]*class="footnote-back"[^>]*>(\d+)</a>'
    )
    fn_sec_rx = re.compile(
        r'<section\s+id="footnotes"\s+class="footnotes footnotes-end-of-document"\s+epub:type="footnotes">'
    )
    guide_rx = re.compile(r'\s*<guide\b[^>]*>.*?</guide>', re.S)
    spine_nav_rx = re.compile(r'\s*<itemref\b[^>]*\bidref="nav"[^>]*/>')

    with zipfile.ZipFile(epub_path, "r") as zin:
        opf_name = next((n for n in zin.namelist() if n.endswith(".opf")), None)
        opf_dir = pathlib.PurePosixPath(opf_name).parent if opf_name else pathlib.PurePosixPath("EPUB")
        note_rel_href = "media/note.png"
        note_zip_path = str(opf_dir / note_rel_href) if str(opf_dir) != "." else note_rel_href

        # First pass: transform XHTML chapters so we know whether note.png is actually referenced
        patched_xhtml = {}
        used_note_icon = False
        for item in zin.infolist():
            if item.filename.endswith(".xhtml") and "text/" in item.filename:
                txt = zin.read(item.filename).decode("utf-8")
                orig = txt
                if "page-break-only" in txt:
                    txt = pb_h1_rx.sub("", txt)
                if footnote_mode in ("dual", "weread") and 'class="footnote-ref"' in txt:
                    # Extract plain text of each footnote from Pandoc's <aside id="fnN">
                    fn_map = {}
                    for m in fn_aside_rx.finditer(txt):
                        num = m.group(1)
                        raw_inner = m.group(2)
                        plain = re.sub(r"<[^>]+>", "", raw_inner)
                        plain = html.unescape(plain)
                        plain = re.sub(r"\s+", " ", plain).strip()
                        plain = re.sub(rf"^{num}\.\s*", "", plain)
                        fn_map[num] = plain
                    if fn_map:
                        def _repl_ref(m):
                            nonlocal used_note_icon
                            head, num, tail_attrs, _, close_a = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
                            body = fn_map.get(num)
                            if body is None:
                                return m.group(0)
                            used_note_icon = True
                            alt_esc = html.escape(body, quote=True)
                            img_tag = f'<img alt="{alt_esc}" class="qqreader-footnote" src="../{note_rel_href}" />'
                            if footnote_mode == "dual":
                                return f'{head} duokan-footnote{tail_attrs}{img_tag}{close_a}'
                            return img_tag
                        txt = fn_ref_full_rx.sub(_repl_ref, txt)
                        if footnote_mode == "weread":
                            # Clean chapter-end fallback notes section (no broken #fnref links)
                            def _clean_fn_section(sm):
                                inner = sm.group(1)
                                inner = re.sub(r'<aside\b[^>]*\bid="fn\d+"[^>]*>', '<div class="fn-item">', inner)
                                inner = inner.replace("</aside>", "</div>")
                                inner = fn_back_rx.sub(r"<strong>\1</strong>", inner)
                                return f'<section class="footnotes footnotes-end-of-document">{inner}</section>'
                            txt = fn_sec_block_rx.sub(_clean_fn_section, txt)
                        elif 'id="footnotes"' in txt:
                            # dual mode preserves all <aside epub:type="footnote" id="fnN"> + backlinks
                            txt = fn_sec_rx.sub('<section class="footnotes footnotes-end-of-document">', txt)
                else:
                    if 'class="footnote-ref"' in txt:
                        txt = fn_ref_rx.sub(r'\1 duokan-footnote\2<sup>\3</sup>\4', txt)
                    if 'id="footnotes"' in txt:
                        txt = fn_sec_rx.sub('<section class="footnotes footnotes-end-of-document">', txt)
                if txt != orig:
                    patched_xhtml[item.filename] = txt.encode("utf-8")

        new_opf = None
        if opf_name:
            opf_text = zin.read(opf_name).decode("utf-8")
            updated_opf = opf_text
            extra_meta = []
            if not re.search(r'<meta\s+[^>]*name="cover"', updated_opf, re.I):
                m = re.search(r'<item\s+[^>]*properties="[^"]*\bcover-image\b[^"]*"[^>]*>', updated_opf)
                if m:
                    id_m = re.search(r'\bid="([^"]+)"', m.group(0))
                    if id_m:
                        extra_meta.append(f'    <meta name="cover" content="{id_m.group(1)}" />')
            if isbn and "urn:isbn:" not in updated_opf.lower():
                clean_isbn = re.sub(r"[^0-9Xx]", "", isbn)
                if len(clean_isbn) in (10, 13):
                    extra_meta.append(f'    <dc:identifier>urn:isbn:{clean_isbn}</dc:identifier>')
            if extra_meta:
                updated_opf = updated_opf.replace("</metadata>", "\n".join(extra_meta) + "\n  </metadata>", 1)
            if used_note_icon and note_rel_href not in updated_opf:
                updated_opf = updated_opf.replace(
                    "</manifest>",
                    f'    <item id="note_icon_png" href="{note_rel_href}" media-type="image/png" />\n  </manifest>',
                    1,
                )
            updated_opf = spine_nav_rx.sub("", updated_opf)
            updated_opf = guide_rx.sub("", updated_opf)
            if updated_opf != opf_text:
                new_opf = updated_opf

        with zipfile.ZipFile(tmp_path, "w") as zout:
            # EPUB spec requires 'mimetype' to be first and uncompressed (ZIP_STORED)
            if "mimetype" in zin.namelist():
                zout.writestr("mimetype", zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for item in zin.infolist():
                if item.filename == "mimetype":
                    continue
                if item.filename == opf_name and new_opf is not None:
                    data = new_opf.encode("utf-8")
                elif item.filename in patched_xhtml:
                    data = patched_xhtml[item.filename]
                else:
                    data = zin.read(item.filename)
                zout.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED)
            if used_note_icon and note_zip_path not in zin.namelist():
                zout.writestr(note_zip_path, _make_note_icon_png_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    tmp_path.replace(epub_path)


CIRCLED_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
FN_LEAD_RX = re.compile(
    rf"^(?P<mark>[{CIRCLED_MARKS}]|\*|\[(\d{{1,2}})\]|［(\d{{1,2}})］|〔(\d{{1,2}})〕|（(\d{{1,2}})）)\s*(?P<body>.+)$",
    re.S,
)


def _split_footnote_entries(text):
    """Split a page_footnote block into [(marker_or_None, body_text), ...]."""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    if not lines:
        return []
    entries = []
    for ln in lines:
        m = FN_LEAD_RX.match(ln)
        if m:
            entries.append([m.group("mark"), m.group("body").strip()])
        elif entries:
            entries[-1][1] += (" " if entries[-1][1][-1:].isascii() and ln[:1].isascii() else "") + ln
        else:
            entries.append([None, ln])
    return entries


def attach_page_footnotes(doc):
    """Match page-bottom `footnote` blocks to same-page inline markers (①–⑳, *, [1], etc.)
    and emit Pandoc Markdown footnote refs `[^fn-pNNNN-k]` WITHOUT mutating `b['text']` in book.json.
    Returns (md_text_map, consumed_block_ids, fn_defs) where:
      - md_text_map: {id(block): text_with_pandoc_fn_refs}
      - consumed_block_ids: set of id(footnote_block) converted to pop-up footnotes
      - fn_defs: list of (fn_id, body_text) in order
    """
    md_text_map = {}
    consumed = set()
    fn_defs = []
    last_fn_idx = None
    last_fn_page = None

    for p in doc.get("pages", []):
        pno = p["page"]
        blocks = p.get("blocks", [])
        fn_blocks = [b for b in blocks if b.get("type") == "footnote" and (b.get("text") or "").strip()]
        if not fn_blocks:
            continue
        body_blocks = [b for b in blocks if b.get("type") in ("text", "title", "list") and (b.get("text") or "").strip()]
        page_fn_seq = 0
        for fb in fn_blocks:
            entries = _split_footnote_entries(fb.get("text") or "")
            if not entries:
                continue
            matched_all = True
            for mark, body in entries:
                if mark is None:
                    # Continuation of previous page's last footnote
                    if last_fn_idx is not None and last_fn_page == pno - 1:
                        prev_id, prev_body = fn_defs[last_fn_idx]
                        sep = " " if (prev_body[-1:].isascii() and body[:1].isascii()) else ""
                        fn_defs[last_fn_idx] = (prev_id, prev_body + sep + body)
                    else:
                        matched_all = False
                    continue

                # Search body/title blocks on the same page for the matching marker
                # Consume any OCR-emitted leading `\` or `^` (e.g. `\*`, `^*`, `^①`) so Pandoc
                # does not see `\[^fn-...]` or `^[^fn-...]` and ignore the footnote ref.
                mark_re = re.compile(r"(?:\\|\^)?" + re.escape(mark))
                fallback_re = None
                if len(mark) == 1 and "\u2460" <= mark <= "\u2473":
                    num = ord(mark) - ord("\u2460") + 1
                    fallback_re = re.compile(r"\^" + str(num) + r"(?!\d)")

                target_b = None
                use_re = mark_re
                for bb in body_blocks:
                    cur = md_text_map.get(id(bb), bb.get("text") or "")
                    if mark_re.search(cur):
                        target_b = bb
                        break
                if target_b is None and fallback_re is not None:
                    for bb in body_blocks:
                        cur = md_text_map.get(id(bb), bb.get("text") or "")
                        if fallback_re.search(cur):
                            target_b = bb
                            use_re = fallback_re
                            break
                if target_b is None:
                    matched_all = False
                    continue

                page_fn_seq += 1
                fn_id = f"fn-p{pno:04d}-{page_fn_seq}"
                cur = md_text_map.get(id(target_b), target_b.get("text") or "")
                # Replace first occurrence of marker with Pandoc footnote syntax `[^fn_id]`
                md_text_map[id(target_b)] = use_re.sub(f"[^{fn_id}]", cur, count=1)
                fn_defs.append((fn_id, body))
                last_fn_idx = len(fn_defs) - 1
                last_fn_page = pno

            if matched_all:
                consumed.add(id(fb))
    return md_text_map, consumed, fn_defs


def is_blockquote_block(b, page_w, frame):
    """Detect whole-block indented academic quotations (e.g. classical Chinese / document quotes)
    from PDF bbox geometry on single-column pages. Avoids normal 1-line paragraphs."""
    if b.get("type") != "text":
        return False
    txt = (b.get("text") or "").strip()
    if len(txt) < 42:
        return False
    x0, y0, x1, y1 = b["bbox"]
    fx0, fx1, fcx, fw = frame
    bw = x1 - x0
    indent_left = x0 - fx0
    return (0.038 * fw <= indent_left <= 0.22 * fw) and (bw >= 0.52 * fw) and (x1 >= fcx + 0.18 * fw)


def extract_copyright_metadata(doc, page_roles):
    """Extract optional publisher, publication date, and ISBN from detected copyright pages."""
    meta = {"publisher": "", "date": "", "isbn": ""}
    cp_pages = [p for p in doc.get("pages", []) if page_roles.get(p["page"]) == "copyright_page"]
    if not cp_pages:
        return meta
    txt = "\n".join(
        (b.get("text") or "").strip()
        for p in cp_pages for b in p.get("blocks", [])
        if (b.get("text") or "").strip()
    )
    m_pub = re.search(r"([\u4e00-\u9fff]{2,14}出版社)", txt)
    if m_pub:
        meta["publisher"] = m_pub.group(1)
    m_date = re.search(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*第?\s*1\s*版", txt)
    if not m_date:
        m_date = re.search(r"出版社[，,]\s*((?:19|20)\d{2})\.(\d{1,2})", txt)
    if m_date:
        meta["date"] = f"{m_date.group(1)}-{int(m_date.group(2)):02d}"
    m_isbn = re.search(r"ISBN\s*([0-9Xx][-0-9Xx\s]{8,20}[0-9Xx])", txt, re.I)
    if m_isbn:
        meta["isbn"] = re.sub(r"\s+", "", m_isbn.group(1))
    return meta


def save_page_image(pdf, pno, out_path, max_h=1600):
    """Save 1-based page `pno` as a clean JPEG image (lossless JPEG passthrough when single DCTDecode & unrotated)."""
    pg = pdf[pno - 1]
    imgs = pg.get_images(full=True)
    if len(imgs) == 1 and imgs[0][8] == "DCTDecode" and pg.rotation == 0:
        pathlib.Path(out_path).write_bytes(pdf.xref_stream_raw(imgs[0][0]))
    else:
        scale = min(150.0 / 72.0, float(max_h) / max(1.0, pg.rect.height))
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
        pix.save(str(out_path), jpg_quality=92)


def compute_book_frame(doc):
    """Estimate book-wide median text-frame [fx0_frac, fx1_frac] separately for odd/even pages
    (accounts for binding gutter shift) and detect whether the book is predominantly multi-column."""
    odd_x0, odd_x1, even_x0, even_x1 = [], [], [], []
    wide_widths = []
    for p in doc.get("pages", []):
        W = max(1.0, p.get("w") or 1.0)
        for b in p.get("blocks", []):
            if b.get("type") != "text":
                continue
            x0, y0, x1, y1 = b["bbox"]
            wr = (x1 - x0) / W
            if wr >= 0.30 and len((b.get("text") or "").strip()) >= 30:
                wide_widths.append(wr)
            if wr >= 0.55:
                if p["page"] % 2 == 1:
                    odd_x0.append(x0 / W); odd_x1.append(x1 / W)
                else:
                    even_x0.append(x0 / W); even_x1.append(x1 / W)

    def med(vals, default):
        if not vals:
            return default
        s = sorted(vals)
        return s[len(s) // 2]

    all_x0 = odd_x0 + even_x0
    all_x1 = odd_x1 + even_x1
    def_x0, def_x1 = med(all_x0, 0.12), med(all_x1, 0.88)
    multi_col_book = bool(wide_widths) and med(wide_widths, 0.75) < 0.50
    return {
        1: (med(odd_x0, def_x0), med(odd_x1, def_x1)),
        0: (med(even_x0, def_x0), med(even_x1, def_x1)),
        "multi_col_book": multi_col_book,
    }


def is_multi_col_page(p):
    """Detect whether page `p` has side-by-side text columns (e.g. 2-column 地方志)."""
    W = max(1.0, p.get("w") or 1.0)
    tbs = [b["bbox"] for b in p.get("blocks", []) if b.get("type") == "text" and (b["bbox"][2] - b["bbox"][0]) < 0.52 * W]
    for i in range(len(tbs)):
        a = tbs[i]
        ah = max(1.0, a[3] - a[1])
        for j in range(i + 1, len(tbs)):
            b = tbs[j]
            ov_y = min(a[3], b[3]) - max(a[1], b[1])
            sep_x = max(b[0] - a[2], a[0] - b[2])
            if ov_y > 0.35 * min(ah, max(1.0, b[3] - b[1])) and sep_x > 0.02 * W:
                return True
    return False


def page_frame(p, book_frame):
    """Return (fx0, fx1, fcx, fw) in PDF points for page `p`."""
    W = max(1.0, p.get("w") or 1.0)
    wx0, wx1 = [], []
    for b in p.get("blocks", []):
        if b.get("type") == "text" and (b["bbox"][2] - b["bbox"][0]) >= 0.55 * W:
            wx0.append(b["bbox"][0]); wx1.append(b["bbox"][2])
    if len(wx0) >= 2:
        wx0.sort(); wx1.sort()
        fx0, fx1 = wx0[len(wx0) // 2], wx1[len(wx1) // 2]
    else:
        f0, f1 = book_frame[p["page"] % 2]
        fx0, fx1 = f0 * W, f1 * W
    return fx0, fx1, (fx0 + fx1) / 2.0, max(1.0, fx1 - fx0)


def infer_block_align(b, page_w, frame, allow_body=True):
    """Infer 'center' | 'right' | 'left' strictly from PDF bbox geometry relative to page frame."""
    x0, y0, x1, y1 = b["bbox"]
    bw = x1 - x0
    cx = (x0 + x1) / 2.0
    fx0, fx1, fcx, fw = frame
    txt = (b.get("text") or "").strip()
    if not allow_body:
        return "left"
    # Centered: narrower than text frame, visibly inset from both sides, center near frame/page midline
    if bw < 0.72 * fw and x0 > fx0 + 0.05 * fw and x1 < fx1 - 0.05 * fw:
        if abs(cx - fcx) <= 0.055 * page_w or abs(cx - 0.5 * page_w) <= 0.045 * page_w:
            return "center"
    # Right-aligned: short attribution / date / signature hugging right edge of text frame
    if bw < 0.58 * fw and len(txt) <= 50 and txt.count("\n") <= 1:
        if x0 > fx0 + 0.25 * fw and x1 >= fx1 - 0.14 * page_w:
            return "right"
    return "left"


CIP_RX = re.compile(r"图书在版编目|CIP\s*数据|中国版本图书馆", re.I)
IMPRINT_KEYS = ("ISBN", "定价", "开本", "印张", "第1版", "印刷", "出版发行", "出版、发行", "版权所有", "责任编辑", "发行经销")
BACK_COVER_RX = re.compile(r"上架建议|条形码|封面设计|装帧设计")
MAKER_RX = re.compile(r"电子书制作|制作者|爱问共享|扫描制作|声明")


def classify_special_pages(doc, pdf, toc_pages_set):
    """Classify front-matter pages (before TOC) and copyright/back-cover pages into layout roles:
      cover | back_cover | title_page | copyright_page | standalone_front
    Body prose & TOC pages are not placed in this dict (they flow normally).
    """
    roles = {}
    pages = doc.get("pages", [])
    if not pages:
        return roles
    n_pages = max(p["page"] for p in pages)
    heights = sorted(p.get("h") or 0 for p in pages)
    med_h = heights[len(heights) // 2] if heights else 0

    # Source PDF outline labels per page
    src_toc_labels = {}
    for _, title, pno in (pdf.get_toc() or []):
        src_toc_labels.setdefault(pno, []).append(re.sub(r"\s+", "", title or ""))

    first_toc_page = min((p for p in toc_pages_set if p <= 35), default=None)
    front_limit = first_toc_page if first_toc_page is not None else min(10, n_pages)

    in_preface = False
    seen_copyright = False
    for p in pages:
        pno = p["page"]
        W, H = max(1.0, p.get("w") or 1.0), max(1.0, p.get("h") or 1.0)
        vis = [b for b in p.get("blocks", []) if b.get("type") not in SKIP]
        full_txt = "\n".join((b.get("text") or "").strip() for b in vis if (b.get("text") or "").strip())
        labels = src_toc_labels.get(pno, [])
        labels_str = " ".join(labels)

        # 1) Cover page (p1): already embedded via --epub-cover-image
        if pno == 1:
            roles[pno] = "cover"
            continue

        # 2) Copyright / CIP page (front-matter OR last 5 pages of the book)
        imprint_hits = sum(1 for k in IMPRINT_KEYS if k in full_txt)
        is_cip = bool(CIP_RX.search(full_txt))
        is_copyright = is_cip or imprint_hits >= 3 or (imprint_hits >= 2 and ("版权" in labels_str or pno <= 8 or pno >= n_pages - 4))
        if is_copyright and (pno < front_limit or pno >= n_pages - 4) and pno not in toc_pages_set:
            roles[pno] = "copyright_page"
            in_preface = False
            if pno < front_limit:
                seen_copyright = True
            continue

        # Only classify remaining front-matter roles strictly before the TOC
        if pno >= front_limit or pno in toc_pages_set:
            continue

        # 3) Misplaced back cover placed right after front cover (p2..p4)
        if 2 <= pno <= 4 and not seen_copyright:
            has_bottom_img = any(b.get("type") == "image" and b["bbox"][1] > 0.60 * H for b in vis)
            is_back = ("封底" in labels_str) or bool(BACK_COVER_RX.search(full_txt)) or (
                "定价" in full_txt and ("ISBN" in full_txt or has_bottom_img)
            )
            if is_back:
                roles[pno] = "back_cover"
                continue

        # 4) Visual title page / inner cover (strictly before copyright page, p2..p6): keep as full-page image
        max_para = max((len((b.get("text") or "").strip()) for b in vis if b.get("type") == "text"), default=0)
        has_img = any(b.get("type") == "image" for b in vis)
        if 2 <= pno <= 6 and not in_preface and not seen_copyright:
            toc_is_title = any(l in ("书名", "扉页", "内封", "题名页", "书名页", "封二") for l in labels)
            visual_title = len(full_txt) <= 180 and max_para <= 60 and len(vis) <= 10 and (
                has_img or sum(1 for b in vis if b.get("type") == "title") >= 2
            ) and not MAKER_RX.search(full_txt)
            if toc_is_title or visual_title:
                roles[pno] = "title_page"
                continue

        # 5) Preface start vs standalone front-matter page (e.g. maker notice, dedication, epigraph)
        starts_preface = any(
            b.get("type") == "title" and GENERAL_FRONT_BACK_RX.search(re.sub(r"\s+", "", b.get("text") or ""))
            for b in vis
        ) or any(re.search(r"序|前言|凡例|导言|引论|绪论", l) for l in labels)
        if starts_preface:
            in_preface = True
            continue

        diff_height = med_h > 0 and abs(H - med_h) > 0.04 * med_h
        is_maker = bool(MAKER_RX.search(full_txt))
        has_non_preface_toc = bool(labels) and not any(re.search(r"序|前言|凡例", l) for l in labels)
        if is_maker or has_non_preface_toc or (diff_height and len(full_txt) < 400):
            roles[pno] = "standalone_front"
            in_preface = False
            continue

        if not in_preface and len(full_txt) <= 320 and max_para <= 90 and len(vis) <= 12:
            roles[pno] = "standalone_front"
            continue

    return roles


def _escape_lines_html(txt):
    lines = [html.escape(l.strip()) for l in (txt or "").split("\n") if l.strip()]
    return "<br/>".join(lines)


def render_copyright_page_html(p, frame):
    """Render a CIP / copyright page as clean, PDF-faithful XHTML without Markdown list mangling."""
    W, H = max(1.0, p.get("w") or 1.0), max(1.0, p.get("h") or 1.0)
    vis = [b for b in p.get("blocks", []) if b.get("type") not in SKIP and (b.get("text") or "").strip()]
    if not vis:
        return ""
    # Split into vertical sections when there is a clear visual gap (e.g. CIP top vs centered imprint bottom)
    sections = [[vis[0]]]
    for b in vis[1:]:
        prev_y1 = max(x["bbox"][3] for x in sections[-1])
        if b["bbox"][1] - prev_y1 > 0.09 * H:
            sections.append([b])
        else:
            sections[-1].append(b)

    parts = ['<div class="front-page copyright-page">']
    for si, sec in enumerate(sections):
        if si > 0:
            parts.append('<hr class="copyright-sep"/>')
        sec_txt = "\n".join((b.get("text") or "") for b in sec)
        is_cip_sec = bool(CIP_RX.search(sec_txt))

        # Check whether this section is predominantly centered on the page midline (classic imprint block)
        centered_cnt = 0
        for b in sec:
            x0, y0, x1, y1 = b["bbox"]
            cx = (x0 + x1) / 2.0
            wr = (x1 - x0) / W
            if abs(cx - 0.5 * W) <= 0.065 * W and wr <= 0.65:
                centered_cnt += 1
        sec_centered = (not is_cip_sec) and (centered_cnt >= max(2, 0.55 * len(sec)))

        cls = "cip-block" if is_cip_sec else ("imprint-block align-center" if sec_centered else "imprint-block")
        parts.append(f'<div class="{cls}">')
        for b in sec:
            txt = (b.get("text") or "").strip()
            body_html = _escape_lines_html(txt)
            if not body_html:
                continue
            if is_cip_sec and b.get("type") == "title":
                parts.append(f'<p class="cip-title">{body_html}</p>')
            elif sec_centered:
                if b.get("type") == "title":
                    parts.append(f'<p class="align-center"><strong>{body_html}</strong></p>')
                else:
                    parts.append(f'<p class="align-center">{body_html}</p>')
            else:
                al = infer_block_align(b, W, frame)
                pcls = "align-center" if al == "center" else ("align-right" if al == "right" else "no-indent")
                if b.get("type") == "title":
                    parts.append(f'<p class="{pcls}"><strong>{body_html}</strong></p>')
                else:
                    parts.append(f'<p class="{pcls}">{body_html}</p>')
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)


def render_standalone_front_html(p, frame):
    """Render a standalone front-matter page (e.g. maker notice, epigraph) preserving PDF block alignment."""
    W = max(1.0, p.get("w") or 1.0)
    vis = [b for b in p.get("blocks", []) if b.get("type") not in SKIP]
    parts = ['<div class="front-page standalone-front">']
    for b in vis:
        t = b.get("type")
        if t == "image" and b.get("image"):
            cap = html.escape((b.get("caption") or "").strip() or "图")
            parts.append(f'<p class="align-center"><img src="{html.escape(b["image"])}" alt="{cap}"/></p>')
            continue
        txt = (b.get("text") or "").strip()
        if not txt:
            continue
        body_html = _escape_lines_html(txt)
        al = infer_block_align(b, W, frame)
        bw_frac = (b["bbox"][2] - b["bbox"][0]) / W
        if al == "center":
            pcls = "align-center"
        elif al == "right":
            pcls = "align-right"
        elif bw_frac >= 0.65 and len(txt) >= 24:
            pcls = "front-prose"
        else:
            pcls = "no-indent"
        if t == "title":
            parts.append(f'<p class="{pcls}"><strong>{body_html}</strong></p>')
        else:
            parts.append(f'<p class="{pcls}">{body_html}</p>')
    parts.append("</div>")
    return "\n".join(parts)


NON_AUTHOR_PAREN_RX = re.compile(
    r"^(?:\d{4}(?:\s*[-—–~]\s*\d{4})?|\d{4}年版|第\d+版|修订.*|精|精装|平装|上|下|中|上册|下册|中册|全\d+册|上下册.*|z-lib\.org|县级|测试样品)$",
    re.I,
)


def extract_author(pdf, src_pdf_path, cli_author=""):
    """Determine EPUB author (<dc:creator>): CLI --author -> source PDF metadata -> trailing '(Author)' in filename."""
    if cli_author and cli_author.strip():
        return cli_author.strip()
    meta_author = ((pdf.metadata or {}).get("author") or "").replace("\x00", "").strip()
    if meta_author and not re.fullmatch(r"[\d\s._-]+", meta_author):
        return meta_author
    stem = pathlib.Path(src_pdf_path).stem.strip()
    m = re.search(r"[（(]([^()（）]{1,60})[）)]\s*$", stem)
    if m:
        cand = m.group(1).strip()
        if cand and not NON_AUTHOR_PAREN_RX.match(cand):
            return cand
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_json"); ap.add_argument("src_pdf"); ap.add_argument("out_dir")
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--no-recrop", action="store_true")
    ap.add_argument("--keep-toc-pages", action="store_true", help="keep scanned paper TOC pages in EPUB body")
    ap.add_argument("--footnote-mode", choices=("dual", "weread", "epub3"), default="dual",
                    help="dual: EPUB 3 noteref+aside wrapping qqreader-footnote icon (default); weread: naked qqreader-footnote icon; epub3: superscript noteref+aside")
    a = ap.parse_args()

    doc = json.loads(pathlib.Path(a.doc_json).read_text(encoding="utf-8"))
    base = pathlib.Path(a.doc_json).parent
    out = pathlib.Path(a.out_dir); (out / "images").mkdir(parents=True, exist_ok=True)
    pdf = pymupdf.open(a.src_pdf)

    # 1) images: native re-crop (or copy the tool's crop)
    stats = []
    for p in doc["pages"]:
        native = None
        for i, b in enumerate(p["blocks"]):
            if b["type"] not in ("image", "table"):
                continue
            stem = out / "images" / f"p{p['page']:04d}_{i:02d}_{b['type']}"
            existing = next((f for ext in (".png", ".jpg", ".jpeg") if (f := stem.with_suffix(ext)).exists()), None)
            if a.no_recrop and existing:
                b["image"] = f"images/{existing.name}"
            elif a.no_recrop and b.get("image"):
                src = base / b["image"]
                dst = stem.with_suffix(src.suffix)
                shutil.copyfile(src, dst)
                b["image"] = f"images/{dst.name}"
            else:
                native = native or NativePage(pdf, p["page"])
                info = native.crop(b["bbox"], str(stem))
                b["image"] = f"images/{pathlib.Path(info['path']).name}"
                b["image_px"] = [info["w"], info["h"]]
                b["image_dpi"] = info["dpi"]
                stats.append(info)

    # 2) detect TOC pages, book text-frame geometry, and front/back-matter roles
    gazetteer = is_gazetteer_book(doc)
    toc_matcher = None if gazetteer else build_toc_matcher(pdf)
    gen_shift = 0 if (gazetteer or toc_matcher) else general_heading_shift(doc)

    toc_line = re.compile(r"(…|\.{3,}|·{3,}|-{3,}|—{2,})\s*\d{1,4}\s*$|\D\s*\d{1,4}\s*$")
    toc_pages_set = set()
    for p in doc["pages"]:
        textual = [b for b in p["blocks"] if b["type"] in ("title", "text", "toc", "list")]
        hits = [b for b in textual if any(toc_line.search(l.strip()) for l in (b.get("text") or "").split("\n"))]
        entries = sum(len([l for l in (b.get("text") or "").split("\n") if toc_line.search(l.strip())]) for b in hits)
        if entries >= 4 and len(hits) >= 0.5 * max(1, len(textual)):
            toc_pages_set.add(p["page"])

    book_frame = compute_book_frame(doc)
    page_roles = classify_special_pages(doc, pdf, toc_pages_set)
    doc["page_roles"] = {str(k): v for k, v in sorted(page_roles.items())}
    cp_meta = extract_copyright_metadata(doc, page_roles)

    # Extract cover (p1) if present
    cover_rel = None
    if pdf.page_count > 0:
        save_page_image(pdf, 1, out / "images" / "cover.jpg")
        cover_rel = "images/cover.jpg"

    stream = []
    deferred_back_covers = []
    for p in doc["pages"]:
        pno = p["page"]
        role = page_roles.get(pno)
        is_toc_page = pno in toc_pages_set
        frame = page_frame(p, book_frame)
        allow_align = (not book_frame["multi_col_book"]) and (not is_multi_col_page(p))

        if is_toc_page:
            for b in p["blocks"]:
                if b["type"] == "text":
                    b["type"] = "toc"

        # Always assign heading levels / clean titles on all pages so book.json & PDF bookmarks stay intact
        for b in p["blocks"]:
            if b["type"] in SKIP:
                continue
            if is_toc_page and b["type"] == "title" and re.sub(r"\s", "", b.get("text", "")) != "目录":
                b["type"] = "toc"            # TOC entries are not headings / bookmarks
            if b["type"] == "toc":
                b["text"] = (b.get("text") or "").strip()
            if b["type"] == "title":
                raw_t = b.get("text", "")
                if gazetteer:
                    b["level"] = heading_level(raw_t, b.get("level"))
                elif toc_matcher is not None:
                    matched = toc_matcher(pno, raw_t)
                    if matched is not None:
                        b["level"] = matched
                    elif re.sub(r"\s+", "", raw_t) == "目录":
                        b["level"] = 1
                    else:
                        b["level"] = max(4, min(6, (b.get("level") or 4) + 3))
                else:
                    b["level"] = general_heading_level(raw_t, b.get("level"), shift=gen_shift)
                b["text"] = clean_title(raw_t)

        # Skip scanned paper TOC pages in EPUB (reader provides native interactive TOC) unless --keep-toc-pages
        if is_toc_page and not a.keep_toc_pages:
            continue

        # Handle special front/back-matter pages for EPUB / Markdown layout
        if role == "cover":
            continue  # Page 1 is already shown as EPUB cover.xhtml
        if role == "back_cover":
            rel = f"images/back_cover_p{pno:04d}.jpg"
            save_page_image(pdf, pno, out / rel)
            deferred_back_covers.append((pno, rel))
            continue
        if role == "title_page":
            rel = f"images/title_page_p{pno:04d}.jpg"
            save_page_image(pdf, pno, out / rel)
            stream.append({"type": "_special_md", "chunks": [
                f"# 扉页p{pno} {{.unlisted .unnumbered .page-break-only}}",
                f'<div class="full-page-img">\n\n![扉页]({rel})\n\n</div>',
            ]})
            continue
        if role == "copyright_page":
            c_html = render_copyright_page_html(p, frame)
            if c_html:
                stream.append({"type": "_special_md", "chunks": [
                    f"# 版权信息p{pno} {{.unlisted .unnumbered .page-break-only}}",
                    c_html,
                ]})
            continue
        if role == "standalone_front":
            s_html = render_standalone_front_html(p, frame)
            if s_html:
                stream.append({"type": "_special_md", "chunks": [
                    f"# 附页p{pno} {{.unlisted .unnumbered .page-break-only}}",
                    s_html,
                ]})
            continue

        # Normal pages (preface, body chapters, and optional TOC pages)
        for b in p["blocks"]:
            if b["type"] in SKIP:
                continue
            b["page"] = pno
            if allow_align and b["type"] in ("text", "list"):
                if is_blockquote_block(b, max(1.0, p.get("w") or 1.0), frame):
                    b["blockquote"] = True
                else:
                    al = infer_block_align(b, max(1.0, p.get("w") or 1.0), frame)
                    if al != "left":
                        b["align"] = al
            # "第二节" + name set as two short stacked lines beside it ("解放后区政 / 府机构设置"):
            # the tool returns the name as separate short text blocks -> fold them into the heading
            prev = stream[-1] if stream and stream[-1].get("type") != "_special_md" else None
            if b["type"] in ("text", "title") and prev and prev["type"] == "title" and prev.get("page") == pno \
                    and (prev.get("_bare") or re.fullmatch(rf"第[{CN}\d]+[编章节]", prev["text"].strip())):
                txt = re.sub(r"\s+", "", b.get("text") or "")
                beside = (b["bbox"][0] >= prev["bbox"][2] - 2 and b["bbox"][1] < prev["bbox"][3] + 15
                          and b["bbox"][3] > prev["bbox"][1] - 15)
                if txt and len(txt) <= 12 and beside:
                    # full heading for Markdown / TOC / bookmarks; `text` keeps what is printed at this bbox
                    prev["heading"] = prev.get("heading", prev["text"].strip() + " ") + txt
                    prev["_bare"] = True
                    b["merged_into_heading"] = True
                    continue
            if prev:
                prev.pop("_bare", None)
            stream.append(b)

    # 3) Convert page-bottom footnotes into EPUB 3 pop-up footnotes without mutating book.json texts
    fn_md_map, fn_consumed, fn_defs = attach_page_footnotes(doc)

    md, last_text, last_table, last_page, last_y0 = [], None, None, None, 0
    joins = 0
    for b in stream:
        t = b["type"]
        if t == "_special_md":
            md.extend(b["chunks"])
            last_text = last_table = None
            continue
        if t == "footnote" and id(b) in fn_consumed:
            # Consumed into an inline pop-up footnote ref: do NOT interrupt cross-page prose!
            b.pop("page", None)
            continue
        raw_txt = fn_md_map.get(id(b), b.get("text") or "")
        txt = raw_txt.strip()
        if t == "title":
            heading_txt = b.get("heading") or txt
            if id(b) in fn_md_map and b.get("heading"):
                # Preserve footnote ref attached to title if heading was merged
                heading_txt = fn_md_map[id(b)].strip()
            md.append("#" * b["level"] + " " + heading_txt)
            last_text = last_table = None
        elif t == "toc":
            md.extend(l.strip() for l in txt.split("\n") if l.strip())
            last_text = None
        elif t in ("text", "list", "footnote"):
            if not txt:
                b.pop("page", None)
                continue
            al = b.get("align", "left")
            is_bq = bool(b.get("blockquote"))
            if t == "footnote":
                # Unmatched fallback footnote: preserve cleanly without losing content
                md.append(f'<p class="unmatched-footnote">{_escape_lines_html(txt)}</p>')
                last_text = None
                b.pop("page", None)
                continue
            prev_is_bq = last_text is not None and md[last_text].startswith("> ")
            same_style = (prev_is_bq == is_bq) and (al == "left")
            # unfinished sentence + this block starts a new column (higher on the page) or a new page
            new_col_or_page = last_page is not None and (b["page"] == last_page + 1
                                                         or (b["page"] == last_page and b["bbox"][1] < last_y0 - 20))
            prev_plain = md[last_text][2:] if prev_is_bq else (md[last_text] if last_text is not None else "")
            heuristic = (last_text is not None and same_style and new_col_or_page and not prev_plain.endswith(SENT_END)
                         and re.search(r"[，。；]", prev_plain) is not None      # prose, not a name list / title page
                         and not re.match(r"^[（(]?[一二三四五六七八九十\d]+[、.．）)](?!\d)", txt))   # "200.98" is not "200."
            # tool flags first; the heuristic catches the breaks MinerU misses (e.g. 日照 p196 column break)
            cont = (bool(b.get("continues_prev")) or heuristic) and same_style
            if last_text is not None and cont:
                md[last_text] += txt          # paragraph continues across column / page break
                b["joined_to_previous"] = True
                joins += 1
            elif is_bq:
                md.append("> " + txt.replace("\n", " "))
                last_text = len(md) - 1
            elif al in ("center", "right"):
                cls = "align-center" if al == "center" else "align-right"
                if "[^fn-" in txt:
                    md.append(f"::: {{.{cls}}}\n\n{txt}\n\n:::")
                else:
                    md.append(f'<p class="{cls}">{_escape_lines_html(txt)}</p>')
                last_text = None
            else:
                md.append(txt)
                last_text = len(md) - 1
            last_page, last_y0 = b["page"], b["bbox"][1]
            if txt.startswith("注"):
                last_text = None
            elif len(txt) > 60:
                last_table = None             # real prose between tables: no longer a continuation
        elif t == "caption":
            if b.get("parent"):
                continue                      # already emitted with its figure / table
            if txt and "续表" not in txt:
                md.append(f"*{txt}*")
        elif t == "image":
            cap = (b.get("caption") or "").strip()
            md.append(f"![{html.escape(cap) or '图'}]({b['image']})")
            if cap:
                md.append(f"*{cap}*")
            # figures do not interrupt a paragraph that flows around them
        elif t == "table":
            xml = xhtml_table(b.get("html") or "") if b.get("html") else None
            cap = (b.get("caption") or "").strip()
            cont = "续表" in cap or b.get("continued") or b.get("continues_prev")
            if cont and last_table is not None and xml and md[last_table[0]].startswith("<table"):
                merged, dropped = merge_continued_table(md[last_table[0]], xml)
                md[last_table[0]] = merged
                b["merged_into_previous_table"] = True
                md.append(f"![续表截图（第{b['page']}页）]({b['image']})")
            else:
                if cap:
                    md.append(f"**{cap}**")
                md.append(f"![表格截图（第{b['page']}页）]({b['image']})")
                if xml:
                    md.append(xml)
                    last_table = (len(md) - 1, b)
            last_text = None
        b.pop("page", None)

    # Append any misplaced back-cover pages at the very end of the EPUB
    for pno, rel in deferred_back_covers:
        md.append(f"# 封底p{pno} {{.unlisted .unnumbered .page-break-only}}")
        md.append(f'<div class="full-page-img">\n\n![封底]({rel})\n\n</div>')

    # phone readers: wide tables scroll sideways inside their own box (and get a smaller font) instead of
    # widening the page. (Linking the screenshot to itself is not possible: pandoc does not rewrite link
    # targets into the EPUB media folder.)
    for k, s in enumerate(md):
        if s.startswith("<table"):
            ncols = max((len(tr) for tr in etree.fromstring(s).iter("tr")), default=0)
            md[k] = f'<div class="tbl{" wide" if ncols >= 10 else ""}">{s}</div>'
        elif not s.startswith(("![", "<div", "<p ", "<hr", ":::")):
            md[k] = sanitize_prose_md(s)

    # Append Pandoc footnote definitions (Pandoc places each chapter's <aside> inside its own chapter XHTML)
    for fn_id, fn_body in fn_defs:
        clean_body = sanitize_prose_md(re.sub(r"\s+", " ", fn_body).strip())
        md.append(f"[^{fn_id}]: {clean_body}")

    title = a.title or doc.get("book", "")
    author = extract_author(pdf, a.src_pdf, a.author)
    fm = [f"title: {json.dumps(title, ensure_ascii=False)}"]
    if author:
        fm.append(f"author: {json.dumps(author, ensure_ascii=False)}")
    if cp_meta.get("publisher"):
        fm.append(f"publisher: {json.dumps(cp_meta['publisher'], ensure_ascii=False)}")
    if cp_meta.get("date"):
        fm.append(f"date: {json.dumps(cp_meta['date'], ensure_ascii=False)}")
    fm.append("lang: zh-CN")
    text = "---\n" + "\n".join(fm) + "\n---\n\n" + "\n\n".join(md) + "\n"
    (out / "book.md").write_text(text, encoding="utf-8")
    (out / "book.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    css = out / "epub.css"
    css.write_text(
        '@charset "utf-8";\n'
        'body{font-family:serif;line-height:1.68;text-align:justify}\n'
        'p{text-indent:2em;margin:0.35em 0}\n'
        'h1{font-size:1.45em;font-weight:bold;line-height:1.35;page-break-before:always;break-before:page;page-break-after:avoid;break-after:avoid;text-align:center;margin:1.5em 0 0.9em}\n'
        'h2{font-size:1.18em;font-weight:bold;line-height:1.4;page-break-after:avoid;break-after:avoid;text-align:center;margin:1.4em 0 0.7em}\n'
        'h3{font-size:1.05em;font-weight:bold;line-height:1.45;page-break-after:avoid;break-after:avoid;text-align:left;text-indent:0;margin:1.1em 0 0.5em}\n'
        'blockquote{margin:0.8em 1.2em;padding:0.4em 0.9em;border-left:3px solid #c7c7c7;font-family:STKaiti,KaiTi,"楷体",serif;font-size:0.93em;line-height:1.6;color:#333}\n'
        'blockquote p{text-indent:2em;margin:0.25em 0}\n'
        '.footnote-ref{text-decoration:none}\n'
        '.footnote-ref sup{font-size:0.75em;vertical-align:super;line-height:0}\n'
        '.unmatched-footnote{font-size:0.85em;color:#555;text-indent:0;margin:0.4em 0 0.4em 1.5em}\n'
        '.align-center{text-align:center;text-indent:0}\n'
        '.align-center p{text-align:center;text-indent:0}\n'
        '.align-right{text-align:right;text-indent:0}\n'
        '.align-right p{text-align:right;text-indent:0}\n'
        '.no-indent{text-indent:0;text-align:left}\n'
        '.front-page{page-break-before:always;break-before:page;page-break-after:always;break-after:page}\n'
        '.front-page p{margin:0.25em 0}\n'
        '.front-page p.front-prose{text-indent:2em;text-align:justify}\n'
        '.copyright-page{font-size:0.85em;line-height:1.55;padding:0.5em 2%}\n'
        '.copyright-page .cip-title{font-weight:bold;text-align:left;text-indent:0;margin:0.2em 0 0.6em}\n'
        '.copyright-page .cip-block{width:92%;margin:0 auto}\n'
        '.copyright-page .imprint-block{margin-top:0.8em}\n'
        '.copyright-sep{border:0;border-top:1px solid #bbb;width:72%;margin:1.2em auto}\n'
        '.full-page-img{page-break-before:always;break-before:page;page-break-after:always;break-after:page;text-align:center;margin:0;padding:0}\n'
        '.full-page-img p{text-indent:0;text-align:center;margin:0}\n'
        '.full-page-img img{max-width:100%;max-height:95vh;width:auto;height:auto;display:block;margin:0 auto}\n'
        'img{max-width:100%;height:auto;display:block;margin:0.5em auto}\n'
        'img.qqreader-footnote{display:inline;width:1.05em;height:1.05em;margin:0 0.12em;vertical-align:-0.15em;max-width:none}\n'
        '.footnotes{font-size:0.86em;color:#444;margin-top:1.4em}\n'
        '.fn-item p{text-indent:0;margin:0.3em 0}\n'
        'table{border-collapse:collapse;font-size:0.8em;margin:0.5em 0}\n'
        '.tbl{overflow-x:auto;max-width:100%}.tbl.wide table{font-size:0.6em}\n'
        'td,th{border:1px solid #888;padding:0.15em 0.3em}\n'
        'em{display:block;text-align:center;font-style:normal;font-size:0.85em}\n',
        encoding="utf-8",
    )
    split_lvl = "2" if gazetteer else "1"
    pandoc_cmd = ["pandoc", "book.md", "-f", "markdown+raw_html-markdown_in_html_blocks-implicit_figures", "-o", "book.epub",
                  "--toc-depth=3", f"--split-level={split_lvl}", "--css", "epub.css",
                  "--metadata", "lang=zh-CN"]
    if cover_rel:
        pandoc_cmd += ["--epub-cover-image", cover_rel]
    subprocess.run(pandoc_cmd, cwd=out, check=True)
    patch_epub_postprocess(out / "book.epub", isbn=cp_meta.get("isbn", ""), footnote_mode=a.footnote_mode)
    print(json.dumps({"out": str(out), "blocks": len(stream), "paragraph_joins": joins,
                      "images": len(stats), "page_roles": doc["page_roles"],
                      "popup_footnotes": len(fn_defs),
                      "blockquotes": sum(1 for b in stream if b.get("blockquote")),
                      "metadata": cp_meta,
                      "headings": sum(1 for b in stream if b.get('type') == 'title')},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
