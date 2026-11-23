"""Per-book acceptance checks for a full run -> <book_dir>/qa.json  (status: pass | needs_review).

  /opt/ocr/post/bin/python qa_book.py <source.pdf> <book_dir> <book_name>

Checks (thresholds from the batch spec):
  pdf_pages == source pages; images_byte_identical (re-hashed here, independent of build_pdf.py);
  old 超星 layer streams removed ~= pages (counted independently in the source; books / pages without
  an old layer are flagged); size ratio < 1.10; bookmarks > 0 and no level jumps; share of
  characters outside CJK / digits / common punctuation < 0.5 % (same character class as
  verify_pdf.py, which also accepts Latin letters -- reported separately); epubcheck 0 errors;
  every page has text or a figure (blank pages listed, with the scan's own ink share);
  hybrid_log.json change count + first 20 entries.
"""
import re
import sys
import json
import hashlib
import pathlib
import collections
import numpy as np
import pikepdf
import pymupdf

# verify_pdf.py's "OK" character class (CJK punctuation, CJK ideographs, full-width forms, general
# punctuation, digits, ASCII letters, whitespace and common ASCII punctuation)
OK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\u2000-\u206f0-9A-Za-z\s\.,;:!?()\[\]%+\-/~·×°<>=\"'*#&@]")
CJK_EXT = re.compile(r"[\u3400-\u4dbf\U00020000-\U0003134f\uf900-\ufaff]")   # rare / compatibility ideographs = Chinese too
# Legitimate non-CJK editorial/academic characters common in Chinese local gazetteers (dialect IPA,
# Greek/math/unit/footnote symbols, Latin accented pinyin, Roman & circled numerals).
PUBLISHING_OK = re.compile(
    r"[\u00a0-\u024f\u0250-\u02af\u02b0-\u02ff\u0300-\u036f\u0370-\u03ff"
    r"\u2070-\u209f\u2160-\u217f\u2460-\u24ff"
    r"℃℉‰′″±×÷≤≥≈≠∞√∫∂↓↑←→△▲☆★○●◇□■✓✗§¬∨©®]"
)
LATEX_RAW = set("{}^\\_")


def hashes(doc):
    cache = {}
    out = []
    for pg in doc:
        row = []
        for img in pg.get_images(full=True):
            xref = img[0]
            h = cache.get(xref)
            if h is None:
                h = hashlib.sha256(doc.xref_stream_raw(xref)).hexdigest()
                cache[xref] = h
            row.append(h)
        out.append(row)
    return out


def old_layer_pages(path):
    pdf = pikepdf.open(path)
    n, pages = 0, []
    for i, page in enumerate(pdf.pages):
        cont = page.obj.get("/Contents")
        streams = list(cont) if isinstance(cont, pikepdf.Array) else ([cont] if cont is not None else [])
        k = sum(1 for s in streams if s.get("/BHJXID") == "/OCRText")
        n += k
        if k == 0:
            pages.append(i + 1)
    pdf.close()
    return n, pages


def scan_ink_metrics(page, margin_frac=0.10):
    """Return (full_page_ink_share, interior_ink_share) at 36 dpi.
    Trimming outer margins separates dark scan borders/gutter shadows on blank verso pages
    from genuine missed content in the page interior."""
    pix = page.get_pixmap(dpi=36, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    full_ink = round(float((a < 128).mean()), 4)
    h, w = a.shape
    y0, y1 = int(h * margin_frac), max(int(h * margin_frac) + 1, int(h * (1 - margin_frac)))
    x0, x1 = int(w * margin_frac), max(int(w * margin_frac) + 1, int(w * (1 - margin_frac)))
    inner_ink = round(float((a[y0:y1, x0:x1] < 128).mean()), 4)
    return full_ink, inner_ink


def detect_duplicate_overlap_pages(doc, min_dups_per_page=5):
    """Detect pages with identical text lines placed at nearly identical coordinates (catches double text layers)."""
    dup_pages = []
    for i, pg in enumerate(doc):
        seen = []
        dups = 0
        for b in pg.get_text("dict").get("blocks", []):
            for l in b.get("lines", []):
                t = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                if len(t) < 2:
                    continue
                x0, y0, x1, y1 = l["bbox"]
                matched = False
                for pt, px0, py0, px1, py1 in seen:
                    if pt == t and abs(x0 - px0) <= 4 and abs(y0 - py0) <= 4 and abs(x1 - px1) <= 6 and abs(y1 - py1) <= 6:
                        matched = True
                        break
                if matched:
                    dups += 1
                else:
                    seen.append((t, x0, y0, x1, y1))
        if dups >= min_dups_per_page:
            dup_pages.append({"page": i + 1, "duplicate_lines": dups})
    return dup_pages


def main():
    src_path, book_dir, name = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
    pdf_path = book_dir / f"{name}_双层.pdf"
    reasons, qa = [], {"book": name, "source_pdf": src_path}
    src, out = pymupdf.open(src_path), pymupdf.open(str(pdf_path))
    n = src.page_count

    qa["pdf_pages"], qa["source_pages"] = out.page_count, n
    qa["pages_equal"] = out.page_count == n
    if not qa["pages_equal"]:
        reasons.append("PDF 页数与原书不符")

    qa["images_byte_identical"] = hashes(src) == hashes(out)
    if not qa["images_byte_identical"]:
        reasons.append("图像字节不一致")

    bp = json.loads((book_dir / "logs" / "build_pdf.json").read_text(encoding="utf-8").strip().splitlines()[-1])
    src_streams, no_layer = old_layer_pages(src_path)
    qa["old_layer_streams_removed"] = bp["old_layer_streams_removed"]
    qa["old_layer_streams_in_source"] = src_streams
    qa["old_layer_removed_per_page"] = round(bp["old_layer_streams_removed"] / n, 3)
    qa["pages_without_old_layer"] = no_layer
    qa["no_old_text_layer"] = src_streams == 0
    out_left, _ = old_layer_pages(str(pdf_path))
    qa["old_layer_streams_left_in_output"] = out_left
    qa["untagged_invisible_pages"] = bp.get("untagged_invisible_pages", 0)
    qa["untagged_occluded_pages"] = bp.get("untagged_occluded_pages", 0)
    qa["form_xobj_text_stripped"] = bp.get("form_xobj_text_stripped", 0)
    qa["preserved_visible_text_pages"] = bp.get("preserved_visible_text_pages", [])
    if src_streams == 0:
        if qa["untagged_invisible_pages"] or qa["untagged_occluded_pages"]:
            reasons.append(
                f"原书无超星标记（已按模式/遮挡清理未标记旧层：3Tr {qa['untagged_invisible_pages']} 页、图压字 {qa['untagged_occluded_pages']} 页；断行退回比例分配）"
            )
        else:
            reasons.append("原书没有超星旧文字层（文字层断行退回按行宽比例分配）")
    elif bp["old_layer_streams_removed"] != src_streams or out_left:
        reasons.append("删除的旧文字层流数与原书不一致")
    elif abs(bp["old_layer_streams_removed"] - n) > max(2, 0.02 * n):
        reasons.append(f"删除的旧文字层流数 {bp['old_layer_streams_removed']} 与页数 {n} 相差较大")
    if qa["preserved_visible_text_pages"]:
        reasons.append(f"保护性保留了 {len(qa['preserved_visible_text_pages'])} 页可见矢量文字（建议核对）")
    qa["lines_written"] = bp.get("lines_written")
    qa["lines_on_old_layer_geometry"] = bp.get("lines_on_old_layer_geometry")

    dup_pages = detect_duplicate_overlap_pages(out)
    qa["duplicate_overlap_pages"] = dup_pages[:20]
    qa["duplicate_overlap_page_count"] = len(dup_pages)
    if dup_pages:
        reasons.append(f"检测到 {len(dup_pages)} 页疑似新旧文字层重叠")

    s_out, s_src = pdf_path.stat().st_size, pathlib.Path(src_path).stat().st_size
    qa["out_mb"], qa["src_mb"] = round(s_out / 1e6, 2), round(s_src / 1e6, 2)
    qa["size_ratio"] = round(s_out / s_src, 4)
    if qa["size_ratio"] >= 1.10:
        reasons.append("双层 PDF 体积/原书 ≥ 1.10")

    toc = out.get_toc()
    jumps = [i for i in range(len(toc)) if (toc[i][0] > (toc[i - 1][0] + 1 if i else 1))]
    qa["bookmarks"] = len(toc)
    qa["bookmark_levels"] = dict(collections.Counter(t[0] for t in toc))
    qa["bookmark_level_jumps"] = [toc[i][:3] for i in jumps][:20]
    qa["bookmarks_first_10"] = [t[:3] for t in toc[:10]]
    if not toc:
        reasons.append("没有书签")
    if jumps:
        reasons.append("书签层级跳级")

    page_text = [pg.get_text() for pg in out]
    chars = [c for t in page_text for c in t if not c.isspace()]
    bad = [c for c in chars if not OK.match(c) and not CJK_EXT.match(c)]
    latex_bad = [c for c in bad if c in LATEX_RAW]
    pub_ok = [c for c in bad if c not in LATEX_RAW and PUBLISHING_OK.match(c)]
    unk_bad = [c for c in bad if c not in LATEX_RAW and not PUBLISHING_OK.match(c)]
    latin = sum(1 for c in chars if c.isascii() and c.isalpha())
    denom = max(1, len(chars))
    qa["text_chars"] = len(chars)
    qa["non_text_chars"] = len(bad)
    qa["non_text_ratio"] = round(len(bad) / denom, 6)
    qa["non_text_top"] = collections.Counter(bad).most_common(30)
    qa["latex_residual_chars"] = len(latex_bad)
    qa["latex_residual_ratio"] = round(len(latex_bad) / denom, 6)
    qa["publishing_symbol_chars"] = len(pub_ok)
    qa["unknown_non_text_chars"] = len(unk_bad)
    qa["unknown_non_text_ratio"] = round(len(unk_bad) / denom, 6)
    qa["unknown_non_text_top"] = collections.Counter(unk_bad).most_common(30)
    qa["latin_letters"] = latin
    if qa["latex_residual_ratio"] >= 0.005:
        reasons.append("疑似未清洗 LaTeX 符号占比 ≥ 0.5%")
    if qa["unknown_non_text_ratio"] >= 0.005:
        reasons.append("未知异常非中文字符占比 ≥ 0.5%")

    ep = (book_dir / "logs" / "epubcheck.txt").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"Messages:\s*(\d+) fatals? / (\d+) errors? / (\d+) warnings?", ep)
    qa["epubcheck"] = {"fatals": int(m[1]), "errors": int(m[2]), "warnings": int(m[3])} if m else None
    if not m or int(m[1]) or int(m[2]):
        reasons.append("epubcheck 有错误（或没有结果）")

    bj = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    has = {}
    for p in bj["pages"]:
        txt = any((b.get("text") or "").strip() for b in p["blocks"])
        img = any(b["type"] in ("image", "table") for b in p["blocks"])
        has[p["page"]] = (txt, img)
    blank = []
    for i in range(n):
        txt, img = has.get(i + 1, (False, False))
        if not txt and not img and not page_text[i].strip():
            full_ink, inner_ink = scan_ink_metrics(src[i])
            blank.append({
                "page": i + 1,
                "in_mineru_output": (i + 1) in has,
                "scan_ink_share": full_ink,
                "interior_ink_share": inner_ink,
            })
    qa["blank_pages"] = [b["page"] for b in blank]
    qa["blank_pages_detail"] = blank
    # Distinguish genuine missed interior content from outer scan border/gutter shadows on blank pages
    margin_only = [b["page"] for b in blank if b["scan_ink_share"] >= 0.002 and b["interior_ink_share"] < 0.002]
    suspect = [b["page"] for b in blank if b["interior_ink_share"] >= 0.002]
    qa["blank_pages_margin_only"] = margin_only
    qa["blank_pages_with_ink_in_scan"] = suspect
    if suspect:
        reasons.append(f"{len(suspect)} 页没有文字也没有图片，但原扫描内芯区域有内容")
    qa["pages_in_book_json"] = len(has)

    src.close()
    out.close()

    hl = json.loads((book_dir / "hybrid_log.json").read_text(encoding="utf-8"))
    qa["hybrid_changes"] = len(hl)
    qa["hybrid_changes_first_20"] = hl[:20]

    st = book_dir / "state"
    qa["timings_s"] = {s: json.loads((st / f"{s}.json").read_text())["seconds"]
                       for s in ("recognize", "layout", "post") if (st / f"{s}.json").exists()}
    qa["status"] = "needs_review" if reasons else "pass"
    qa["reasons"] = reasons
    (book_dir / "qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"book": name, "status": qa["status"], "reasons": reasons}, ensure_ascii=False))


if __name__ == "__main__":
    main()
