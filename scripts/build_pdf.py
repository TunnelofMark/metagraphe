"""Build a searchable two-layer PDF from the original scan PDF + a normalized doc.json.

- The original page objects / image XObjects are kept byte-for-byte (verified by hashing).
- The old 超星 OCR layer (content stream tagged /BHJXID /OCRText) and its fonts are dropped.
- New invisible text (render mode 3) is written per line, horizontally fitted to the line box,
  with one subset-embedded CJK font for the whole document.
- Bookmarks are generated from heading blocks (编/章/节) in doc.json.

Usage: python build_pdf.py <source.pdf> <doc.json> <out.pdf> [--pages 1-12,99-110]
If --pages is given, only those original pages are kept (test samples); doc.json page numbers
always refer to the original PDF.
"""
import os
import sys
import json
import hashlib
import argparse
import pathlib
import tempfile
import pikepdf
import pymupdf
from textlayer import TEXTUAL, block_lines, reference_lines

FONT = pymupdf.Font("cjk")   # Droid Sans Fallback (bundled with PyMuPDF), subset on save
RMODE = 0 if os.environ.get("TEXTLAYER_VISIBLE") else 3   # debug: draw the text layer visibly (red)
TEXT_SHOW_OPS = {"Tj", "TJ", "'", '"'}


def parse_ranges(s):
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def image_hashes(pdf_path, pages=None):
    """sha256 of raw (still-encoded) image streams per page, to prove images are untouched."""
    doc = pymupdf.open(pdf_path)
    out = []
    cache = {}
    for i in (pages or range(doc.page_count)):
        row = []
        for img in doc[i].get_images(full=True):
            xref = img[0]
            h = cache.get(xref)
            if h is None:
                h = hashlib.sha256(doc.xref_stream_raw(xref)).hexdigest()
                cache[xref] = h
            row.append(h)
        out.append(row)
    return out


def _opaque_fullpage_xobj_names(pdf_page, mupdf_page, min_cover=0.90):
    """Return Image XObject names (e.g. '/Im1') that are opaque and cover >= min_cover of the page."""
    page_area = mupdf_page.rect.get_area()
    if page_area <= 0:
        return set()
    res = pdf_page.obj.get("/Resources")
    xobjs = res.get("/XObject") if res else None
    if not xobjs:
        return set()
    opaque_names = set()
    for name, obj in xobjs.items():
        if obj.get("/Subtype") != "/Image":
            continue
        if obj.get("/SMask") is not None or obj.get("/Mask") is not None or bool(obj.get("/ImageMask", False)):
            continue
        opaque_names.add(str(name))
    if not opaque_names:
        return set()
    full_names = set()
    for img in mupdf_page.get_images(full=True):
        # img tuple: (xref, smask, w, h, bpc, cs, alt, name, filter, ...)
        nm = "/" + str(img[7]).lstrip("/")
        if nm not in opaque_names:
            continue
        try:
            bbox = mupdf_page.get_image_bbox(img)
            cover = (bbox & mupdf_page.rect).get_area() / page_area
            if cover >= min_cover:
                full_names.add(nm)
        except Exception:
            continue
    return full_names


def _strip_hidden_text_ops(ops, last_opaque_do_idx=-1):
    """OCRmyPDF-inspired graphics-state-aware invisible/occluded text stripper.

    Drops text-showing ops that are either:
      1) rendered in PDF Text Render Mode 3 (`3 Tr`, invisible OCR layer), OR
      2) drawn strictly before a full-page opaque scan image (`Do`) that completely occludes them.
    Visible text (`Tr != 3` and not occluded by a full-page opaque image) is preserved intact.
    Returns (new_ops, stripped_invisible, stripped_occluded, kept_visible).
    """
    new_ops = []
    tr_stack = []
    cur_tr = 0
    in_bt = False
    bt_ops = []
    bt_kept_show = 0
    bt_stripped_inv = 0
    bt_stripped_occ = 0

    stripped_inv = 0
    stripped_occ = 0
    kept_vis = 0

    for idx, (operands, operator) in enumerate(ops):
        op = str(operator)
        if op == "q":
            tr_stack.append(cur_tr)
            (bt_ops if in_bt else new_ops).append((operands, operator))
            continue
        if op == "Q":
            cur_tr = tr_stack.pop() if tr_stack else 0
            (bt_ops if in_bt else new_ops).append((operands, operator))
            continue
        if op == "Tr" and operands:
            try:
                cur_tr = int(operands[0])
            except Exception:
                pass
            (bt_ops if in_bt else new_ops).append((operands, operator))
            continue
        if op == "BT":
            in_bt = True
            bt_ops = [(operands, operator)]
            bt_kept_show = 0
            bt_stripped_inv = 0
            bt_stripped_occ = 0
            continue
        if op == "ET" and in_bt:
            bt_ops.append((operands, operator))
            in_bt = False
            if bt_kept_show > 0:
                new_ops.extend(bt_ops)
                kept_vis += bt_kept_show
            elif bt_stripped_inv == 0 and bt_stripped_occ == 0:
                # Non-text-showing BT..ET (harmless empty block); keep unless we stripped text
                new_ops.extend(bt_ops)
            stripped_inv += bt_stripped_inv
            stripped_occ += bt_stripped_occ
            bt_ops = []
            continue
        if in_bt:
            if op in TEXT_SHOW_OPS:
                if cur_tr == 3:
                    bt_stripped_inv += 1
                    continue
                if idx < last_opaque_do_idx:
                    bt_stripped_occ += 1
                    continue
                bt_kept_show += 1
            bt_ops.append((operands, operator))
        else:
            new_ops.append((operands, operator))

    if in_bt and bt_ops:
        new_ops.extend(bt_ops)
    return new_ops, stripped_inv, stripped_occ, kept_vis


def _strip_form_xobjects(page, seen=None):
    """Strip `3 Tr` invisible text inside Form XObjects without disturbing visible text."""
    seen = seen if seen is not None else set()
    res = page.obj.get("/Resources") if hasattr(page, "obj") else page.get("/Resources")
    xobjs = res.get("/XObject") if res else None
    if not xobjs:
        return 0, False
    stripped_forms = 0
    has_visible = False
    for _, obj in xobjs.items():
        if obj.get("/Subtype") != "/Form":
            continue
        key = obj.objgen
        if key in seen:
            continue
        seen.add(key)
        raw = obj.read_bytes()
        if b"BT" in raw:
            ops = pikepdf.parse_content_stream(obj)
            new_ops, s_inv, _, k_vis = _strip_hidden_text_ops(ops, last_opaque_do_idx=-1)
            if s_inv > 0:
                obj.write(pikepdf.unparse_content_stream(new_ops))
                stripped_forms += 1
            if k_vis > 0:
                has_visible = True
        sub_s, sub_v = _strip_form_xobjects(obj, seen)
        stripped_forms += sub_s
        has_visible = has_visible or sub_v
    return stripped_forms, has_visible


def strip_old_layer(src, keep_pages, tmp):
    """Copy source PDF, keep selected pages, drop /OCRText streams and hidden (3 Tr or scan-occluded) text.
    Preserves visible vector text (`Tr != 3` not occluded by a full-page scan) and non-text ExtGState."""
    pdf = pikepdf.open(src)
    mupdf_src = pymupdf.open(src)
    orig_indices = [p - 1 for p in keep_pages] if keep_pages else list(range(len(pdf.pages)))
    if keep_pages:
        keep = set(orig_indices)
        for i in reversed(range(len(pdf.pages))):
            if i not in keep:
                del pdf.pages[i]

    removed = 0
    untagged_inv_pages = 0
    untagged_occ_pages = 0
    form_xobj_stripped = 0
    preserved_vis_pages = []

    for pos, page in enumerate(pdf.pages):
        orig_pno = orig_indices[pos] + 1
        cont = page.obj.get("/Contents")
        streams = list(cont) if isinstance(cont, pikepdf.Array) else ([cont] if cont is not None else [])
        kept = [s for s in streams if s.get("/BHJXID") != "/OCRText"]
        dropped = len(streams) - len(kept)
        removed += dropped
        if kept:
            page.obj.Contents = pikepdf.Array(kept) if len(kept) > 1 else kept[0]
        elif dropped > 0:
            page.obj.Contents = pdf.make_stream(b"")

        page_has_visible_text = False
        if any(b"BT" in s.read_bytes() for s in kept):
            ops = pikepdf.parse_content_stream(page)
            full_opaque = _opaque_fullpage_xobj_names(page, mupdf_src[orig_pno - 1])
            last_opaque_do = -1
            if full_opaque:
                for idx, (operands, operator) in enumerate(ops):
                    if str(operator) == "Do" and operands and str(operands[0]) in full_opaque:
                        last_opaque_do = idx
            new_ops, s_inv, s_occ, k_vis = _strip_hidden_text_ops(ops, last_opaque_do_idx=last_opaque_do)
            if s_inv > 0 or s_occ > 0:
                page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
                if s_inv > 0:
                    untagged_inv_pages += 1
                if s_occ > 0:
                    untagged_occ_pages += 1
            if k_vis > 0:
                page_has_visible_text = True
                preserved_vis_pages.append(orig_pno)

        f_stripped, f_vis = _strip_form_xobjects(page)
        form_xobj_stripped += f_stripped
        if f_vis:
            page_has_visible_text = True
            if orig_pno not in preserved_vis_pages:
                preserved_vis_pages.append(orig_pno)

        res = page.obj.get("/Resources")
        if res and not page_has_visible_text and "/Font" in res:
            del res["/Font"]

    mupdf_src.close()
    pdf.remove_unreferenced_resources()
    pdf.save(tmp)
    pdf.close()
    return {
        "old_layer_streams_removed": removed,
        "untagged_invisible_pages": untagged_inv_pages,
        "untagged_occluded_pages": untagged_occ_pages,
        "form_xobj_text_stripped": form_xobj_stripped,
        "preserved_visible_text_pages": preserved_vis_pages,
    }


def write_line(tw, page, box, text, vertical=False):
    """Append one invisible line, scaled so its advance spans the box width."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or not text:
        return
    if vertical:
        # vertical text: stack glyphs down the box
        fs = max(1.0, min(w, h / len(text)))
        for k, ch in enumerate(text):
            tw.append((x0, y0 + fs * (k + 0.88)), ch, font=FONT, fontsize=fs)
        return
    nat = FONT.text_length(text.rstrip(), fontsize=1)   # a trailing cell separator does not count
    fs = w / nat if nat else h
    fs = max(1.0, min(fs, h * 1.6))
    # baseline ~ 88% down the line box for CJK glyphs
    tw.append((x0, y0 + h * 0.88), text, font=FONT, fontsize=fs)


def write_rotated(page, lines):
    rot, M = page.rotation, page.derotation_matrix
    page.set_rotation(0)
    page.insert_font(fontname="ocrcjk", fontbuffer=FONT.buffer)
    for ln in lines:
        x0, y0, x1, y1 = ln["bbox"]
        text = ln["text"]
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0 or not text:
            continue
        if ln.get("vertical"):
            fs = max(1.0, min(w, h / len(text)))
            for k, ch in enumerate(text):
                page.insert_text(pymupdf.Point(x0, y0 + fs * (k + 0.88)) * M, ch, fontname="ocrcjk",
                                 fontsize=fs, rotate=rot, render_mode=RMODE, color=(1, 0, 0))
            continue
        nat = FONT.text_length(text.rstrip(), fontsize=1)   # a trailing cell separator does not count
        fs = max(1.0, min(w / nat if nat else h, h * 1.6))
        page.insert_text(pymupdf.Point(x0, y0 + h * 0.88) * M, text, fontname="ocrcjk",
                         fontsize=fs, rotate=rot, render_mode=RMODE, color=(1, 0, 0))
    page.set_rotation(rot)
    page.clean_contents()          # each insert_text call adds a content stream; merge them into one


def heading_toc(doc_json, page_map):
    toc = []
    for p in doc_json["pages"]:
        if p["page"] not in page_map:
            continue
        for b in p["blocks"]:
            # bookmarks: 编/章/节 only (levels 1-3); 一、（一） and front-matter captions stay out
            if b["type"] == "title" and b.get("level") and b["level"] <= 3 and b.get("text", "").strip():
                toc.append([b["level"], (b.get("heading") or b["text"]).strip(), page_map[p["page"]] + 1, b["bbox"][1]])
    # PyMuPDF needs depth to grow by at most 1: nest by the stack of open headings, so a book (or excerpt)
    # that starts at 章 gets 章=1, 节=2 consistently instead of drifting levels
    fixed, stack = [], []
    for lvl, title, pno, y in toc:
        while stack and stack[-1] >= lvl:
            stack.pop()
        stack.append(lvl)
        fixed.append([len(stack), title, pno, {"kind": pymupdf.LINK_GOTO, "page": pno - 1, "to": pymupdf.Point(0, y), "zoom": 0}])
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("doc_json"); ap.add_argument("out")
    ap.add_argument("--pages")
    ap.add_argument("--no-ref-lines", action="store_true", help="ignore the old text layer's line geometry")
    a = ap.parse_args()
    doc_json = json.loads(pathlib.Path(a.doc_json).read_text(encoding="utf-8"))
    keep = parse_ranges(a.pages) if a.pages else None

    with tempfile.TemporaryDirectory(prefix="build_pdf_") as tmp_dir:
        tmp = pathlib.Path(tmp_dir) / "stripped.pdf"
        strip_info = strip_old_layer(a.src, keep, tmp)
        doc = pymupdf.open(tmp)
        orig_numbers = keep or list(range(1, doc.page_count + 1))
        page_map = {pno: i for i, pno in enumerate(orig_numbers)}
        preserved_set = set(strip_info["preserved_visible_text_pages"])

        n_lines, n_snapped = 0, 0
        src_doc = pymupdf.open(a.src)          # still has the old 超星 layer: borrow its line geometry
        for p in doc_json["pages"]:
            if p["page"] not in page_map:
                continue
            # If the page already has preserved visible vector text, still skip overlapping identical
            # areas? For scanned books, preserved visible text is rare (e.g. cover/stamp); write normally
            # unless the page has no scan image at all (pure born-digital page).
            page = doc[page_map[p["page"]]]
            if p["page"] in preserved_set and not page.get_images(full=True):
                continue
            refs = None if a.no_ref_lines else reference_lines(src_doc[p["page"] - 1])
            lines = []
            for blk in p["blocks"]:
                if blk["type"] in TEXTUAL:
                    bl = block_lines(page, blk, refs)
                    n_snapped += sum(1 for l in bl if refs and any(l["bbox"] == r["bbox"] for r in refs))
                    lines += bl
            n_lines += len(lines)
            if page.rotation == 0:
                tw = pymupdf.TextWriter(page.rect)
                for ln in lines:
                    write_line(tw, page, ln["bbox"], ln["text"], ln.get("vertical", False))
                tw.write_text(page, render_mode=RMODE, color=(1, 0, 0))
            else:
                # PyMuPDF 2.0 drops text beyond the unrotated page width on rotated pages; write in
                # unrotated space with rotated glyphs instead, then restore /Rotate.
                write_rotated(page, lines)

        toc = heading_toc(doc_json, page_map)
        doc.set_toc(toc)
        doc.subset_fonts()
        doc.save(a.out, garbage=3, deflate=True, use_objstms=1)
        doc.close()
        src_doc.close()

    # verification: images untouched
    src_idx = [p - 1 for p in orig_numbers]
    same = image_hashes(a.src, src_idx) == image_hashes(a.out)
    src_size = pathlib.Path(a.src).stat().st_size
    print(json.dumps({"out": a.out, "pages": len(orig_numbers),
                      "old_layer_streams_removed": strip_info["old_layer_streams_removed"],
                      "untagged_invisible_pages": strip_info["untagged_invisible_pages"],
                      "untagged_occluded_pages": strip_info["untagged_occluded_pages"],
                      "form_xobj_text_stripped": strip_info["form_xobj_text_stripped"],
                      "preserved_visible_text_pages": strip_info["preserved_visible_text_pages"],
                      "lines_written": n_lines, "lines_on_old_layer_geometry": n_snapped,
                      "bookmarks": len(toc), "images_byte_identical": same,
                      "out_mb": round(pathlib.Path(a.out).stat().st_size / 1e6, 2),
                      "src_mb": round(src_size / 1e6, 2)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
