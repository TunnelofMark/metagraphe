#!/usr/bin/env python3
"""Isolated regression tests for the OCRmyPDF / Marker / Docling-inspired pipeline refinements.

Runs entirely inside a temporary directory (auto-cleaned on exit) and NEVER modifies
original PDFs under /mnt/h or existing outputs under ocr_full/.

Usage:
  /opt/ocr/post/bin/python scripts/test_pipeline_fixes.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pikepdf
import pymupdf

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import adapt_mineru
import assemble
import build_pdf
import qa_book
import recrop
import review_suspects
import textlayer


def make_synthetic_mixed_pdf(path):
    """Create a 2-page PDF:
    - Page 1: opaque full-page image + occluded 0 Tr legacy OCR before Do + 3 Tr invisible OCR after Do + visible 0 Tr title after Do.
    - Page 2: pure born-digital page with visible 0 Tr text + a Form XObject containing 3 Tr hidden text.
    """
    doc = pymupdf.open()
    # Page 1: embed a small solid RGB PNG scaled to cover 100% of 400x600 page
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (245, 245, 245)).save(buf, format="PNG")
    p1 = doc.new_page(width=400, height=600)
    # Draw occluded 0 Tr text BEFORE image
    p1.insert_text((30, 100), "OCCLUDED_OLD_OCR", fontsize=12, render_mode=0)
    # Paint opaque full-page image over it
    p1.insert_image(p1.rect, stream=buf.getvalue())
    # Draw invisible 3 Tr text AFTER image
    p1.insert_text((30, 200), "INVISIBLE_3TR_OLD_OCR", fontsize=12, render_mode=3)
    # Draw genuine visible 0 Tr text AFTER image (must be preserved!)
    p1.insert_text((30, 300), "VISIBLE_COVER_TITLE", fontsize=14, render_mode=0)

    # Page 2: born-digital visible text
    p2 = doc.new_page(width=400, height=600)
    p2.insert_text((30, 100), "BORN_DIGITAL_TEXT", fontsize=14, render_mode=0)
    doc.save(str(path))
    doc.close()

    # Inject a Form XObject with 3 Tr hidden text onto Page 2 via pikepdf
    pdf = pikepdf.open(str(path), allow_overwriting_input=True)
    p2_obj = pdf.pages[1].obj
    f_res = pikepdf.Dictionary(Font=p2_obj.Resources.Font)
    form = pdf.make_stream(b"BT /Helv 12 Tf 3 Tr 30 200 Td (FORM_HIDDEN_3TR) Tj ET")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, 400, 600])
    form.Resources = f_res
    xobjs = p2_obj.Resources.get("/XObject") or pikepdf.Dictionary()
    xobjs["/FmHidden"] = form
    p2_obj.Resources["/XObject"] = xobjs
    cont = p2_obj.Contents
    streams = list(cont) if isinstance(cont, pikepdf.Array) else [cont]
    old_stream = b"\n".join(s.read_bytes() for s in streams)
    p2_obj.Contents = pdf.make_stream(old_stream + b"\nq /FmHidden Do Q\n")
    pdf.save(str(path))
    pdf.close()


def test_strip_old_layer_synthetic(tmp):
    src = tmp / "synthetic.pdf"
    out = tmp / "synthetic_stripped.pdf"
    make_synthetic_mixed_pdf(src)
    info = build_pdf.strip_old_layer(str(src), None, str(out))
    doc = pymupdf.open(str(out))
    t1 = doc[0].get_text()
    t2 = doc[1].get_text()
    doc.close()
    assert "OCCLUDED_OLD_OCR" not in t1, f"Occluded text leaked: {t1!r}"
    assert "INVISIBLE_3TR_OLD_OCR" not in t1, f"3 Tr invisible text leaked: {t1!r}"
    assert "VISIBLE_COVER_TITLE" in t1, f"Visible text was wrongly erased: {t1!r}"
    assert "BORN_DIGITAL_TEXT" in t2, f"Born-digital text was wrongly erased: {t2!r}"
    assert "FORM_HIDDEN_3TR" not in t2, f"Form XObject 3 Tr text leaked: {t2!r}"
    assert info["untagged_invisible_pages"] >= 1
    assert info["untagged_occluded_pages"] >= 1
    assert info["form_xobj_text_stripped"] == 1
    assert 1 in info["preserved_visible_text_pages"] and 2 in info["preserved_visible_text_pages"]
    print("[PASS] synthetic strip_old_layer (3 Tr + occluded 0 Tr + visible text protection + Form XObject)")


def test_strip_old_layer_real_books(tmp):
    cases = [
        ("极简上海志 (3 Tr)", "/mnt/h/电子书/地方志/上海/极简上海志.pdf", [2, 3], "untagged_invisible_pages"),
        ("岚山志 (0 Tr 图压字)", "/mnt/h/电子书/地方志/山东/XFZ29932.岚山志(1840-2004).pdf", [2, 3], "untagged_occluded_pages"),
        ("西岗区志 (/BHJXID /OCRText)", "/mnt/h/电子书/地方志/大连/XFZ23703.西岗区志.pdf", [2, 3], "old_layer_streams_removed"),
    ]
    for label, src_path, pages, expect_key in cases:
        if not pathlib.Path(src_path).exists():
            print(f"[SKIP] {label}: source not found")
            continue
        out = tmp / f"real_{expect_key}.pdf"
        info = build_pdf.strip_old_layer(src_path, pages, str(out))
        doc = pymupdf.open(str(out))
        extracted = "".join(pg.get_text().strip() for pg in doc)
        doc.close()
        assert extracted == "", f"{label}: leftover text after strip: {extracted[:100]!r}"
        assert info[expect_key] >= 2, f"{label}: expected {expect_key}>=2, got {info}"
        assert info["preserved_visible_text_pages"] == [], f"{label}: unexpected visible text: {info}"
        assert build_pdf.image_hashes(src_path, [p - 1 for p in pages]) == build_pdf.image_hashes(str(out))
        print(f"[PASS] real book strip_old_layer: {label} -> {info}")


def test_build_pdf_tmp_cleanup(tmp):
    src = pathlib.Path("/mnt/h/电子书/地方志/大连/XFZ23703.西岗区志.pdf")
    bj = ROOT / "ocr_full" / "XFZ23703.西岗区志" / "book.json"
    if not (src.exists() and bj.exists()):
        print("[SKIP] build_pdf tmp cleanup: sample book missing")
        return
    before = {p.name for p in pathlib.Path("/tmp").glob("build_pdf_*")}
    out_pdf = tmp / "xigang_p2_3.pdf"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_pdf.py"), str(src), str(bj), str(out_pdf), "--pages", "2-3"],
        capture_output=True, text=True, check=True,
    )
    after = {p.name for p in pathlib.Path("/tmp").glob("build_pdf_*")}
    assert after == before, f"Leaked /tmp dirs: {after - before}"
    meta = json.loads(r.stdout.strip().splitlines()[-1])
    assert meta["images_byte_identical"] is True
    assert meta["old_layer_streams_removed"] == 2
    print(f"[PASS] build_pdf /tmp auto-cleanup & byte-identical images -> {meta}")


def test_equation_inline_and_table_continuation(tmp):
    # Unit test on text_of & clean_equation
    sample_content = [
        {"type": "text", "content": "气温 本市年平均气温为 "},
        {"type": "equation_inline", "content": r"12.6^{\circ}C"},
        {"type": "text", "content": " ，年际变动一般在 "},
        {"type": "equation_inline", "content": r"12.1 \sim 13.2^{\circ}C"},
        {"type": "text", "content": " "},
        {"type": "equation_inline", "content": r"^{①}"},
        {"type": "text", "content": " ，走向 "},
        {"type": "equation_inline", "content": r"290^{\circ} \sim 335^{\circ}"},
        {"type": "text", "content": " ，含 "},
        {"type": "equation_inline", "content": r"[\tilde{\alpha}]"},
        {"type": "text", "content": " 与 "},
        {"type": "equation_inline", "content": r"\geqslant 10^{\circ}C"},
    ]
    joined = textlayer.clean_text(adapt_mineru.text_of(sample_content))
    assert "\n" not in joined, f"Spurious newline inside inline paragraph: {joined!r}"
    for bad in ("{", "}", "\\", "^"):
        assert bad not in joined, f"Raw LaTeX {bad!r} leaked in: {joined!r}"
    assert "12.6°C" in joined and "12.1 ~ 13.2°C" in joined and "①" in joined and "≥ 10°C" in joined
    print(f"[PASS] inline equation de-LaTeX & inline join -> {joined}")

    # Real 日照市志 middle_json.json adapter test in isolated tmp folder
    rizhao_work = ROOT / "ocr_full" / "XFZ29417.日照市志" / "work"
    if (rizhao_work / "raw" / "middle_json.json").exists():
        w = tmp / "rizhao_work"
        w.mkdir()
        os.symlink(rizhao_work / "raw", w / "raw")
        subprocess.run(
            [sys.executable, str(SCRIPTS / "adapt_mineru.py"), str(w), str(rizhao_work / "pages.json")],
            check=True, capture_output=True, text=True,
        )
        doc = json.loads((w / "doc.json").read_text(encoding="utf-8"))
        # Count raw LaTeX chars before vs after
        old_doc = json.loads((rizhao_work / "doc.json").read_text(encoding="utf-8"))
        def count_latex(d):
            return sum(
                sum(1 for c in (b.get("text") or "") if c in "{}^\\")
                for p in d["pages"] for b in p["blocks"]
            )
        new_n = count_latex(doc)
        table_cont = sum(1 for p in doc["pages"] for b in p["blocks"] if b["type"] == "table" and b.get("continues_prev"))
        assert new_n <= 1500, f"Expected raw LaTeX chars <= 1500 (was 6328 before fix), got {new_n}"
        assert table_cont == 61, f"Expected 61 continued tables preserved, got {table_cont}"
        print(f"[PASS] real 日照市志 adapt_mineru: raw LaTeX chars={new_n} (<=1500), table continues_prev={table_cont}")


def test_xhtml_table_and_epubcheck(tmp):
    raw_tbl = '<table><tr><th>年份<br/>备注</th><td>10<sup>2</sup><eq>12.6°C</eq><img src=""/></td></tr></table>'
    xml = assemble.xhtml_table(raw_tbl)
    assert "<sup>2</sup>" in xml and "<br/>" in xml and "<eq" not in xml and "<img" not in xml, f"Unexpected table XML: {xml}"
    # Build tiny EPUB with pandoc + run epubcheck
    md = tmp / "book.md"
    css = tmp / "epub.css"
    epub = tmp / "book.epub"
    md.write_text(f"---\ntitle: 测试书\nlang: zh-CN\n---\n\n# 第一编 测试\n\n{assemble.sanitize_prose_md('依据<通告>和[1986](78号)，年均温12.6°C①。')}\n\n<div class=\"tbl\">{xml}</div>\n", encoding="utf-8")
    css.write_text("body{font-family:serif}", encoding="utf-8")
    subprocess.run(
        ["pandoc", "book.md", "-f", "markdown+raw_html-markdown_in_html_blocks-implicit_figures",
         "-o", "book.epub", "--toc", "--toc-depth=3", "--split-level=2", "--css", "epub.css", "--metadata", "lang=zh-CN"],
        cwd=tmp, check=True, capture_output=True, text=True,
    )
    ep = subprocess.run(
        ["java", "-jar", "/opt/ocr/epubcheck/epubcheck.jar", str(epub)],
        capture_output=True, text=True,
    )
    out_txt = ep.stdout + ep.stderr
    assert "0 fatals / 0 errors / 0 warnings" in out_txt, f"epubcheck failed: {out_txt}"
    print("[PASS] assemble.xhtml_table (preserves sup/br, strips <eq>/empty img) + epubcheck 0/0/0")


def test_recrop_multi_image_fallback(tmp):
    from PIL import Image
    import io
    pdf_p = tmp / "multi_img.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=400, height=600)
    for y in (50, 300):
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), (180, 200, 220)).save(buf, format="PNG")
        pg.insert_image(pymupdf.Rect(50, y, 250, y + 150), stream=buf.getvalue())
    doc.save(str(pdf_p))
    doc.close()
    doc = pymupdf.open(str(pdf_p))
    np_pg = recrop.NativePage(doc, 1)
    info = np_pg.crop([50, 50, 250, 200], str(tmp / "crop_multi"))
    doc.close()
    assert info.get("fallback_render") is True and pathlib.Path(info["path"]).exists()
    print(f"[PASS] recrop multi-image fallback -> {info}")


def test_review_suspects_cangshan(tmp):
    book_dir = ROOT / "ocr_full" / "XFZ29383.苍山县志"
    if not book_dir.exists():
        print("[SKIP] review_suspects: 苍山县志 not found")
        return
    out_dir = tmp / "review_cangshan"
    q = review_suspects.triage_book(book_dir)
    qj, rh = review_suspects.render_review_html(q, out_dir, max_thumbs=6)
    assert qj.exists() and rh.exists()
    assert len(q["missed_content_blank"]) == 0, f"Unexpected missed content: {q['missed_content_blank']}"
    assert len(q["margin_only_blank"]) == 52, f"Expected 52 margin-only blank pages, got {len(q['margin_only_blank'])}"
    print(f"[PASS] review_suspects on 苍山县志: missed_content=0, margin_only_blank={len(q['margin_only_blank'])}, report={rh.name}")


def test_epub_quality_fixes():
    # 1) Residual LaTeX cleanup (\left/\right, array/aligned, \Omega, bare \text/\mathrm/\mathbf)
    s1 = textlayer.clean_residual_latex(r"号码儿\left[xo^412mar^35\right] 与 10^-3 ~ 10^5\Omega 和 2\mathrm m^2")
    assert r"\left" not in s1 and r"\right" not in s1 and r"\Omega" not in s1 and r"\mathrm" not in s1, s1
    assert "[xo^412mar^35]" in s1 and "Ω" in s1 and "2m^2" in s1 or "2 m^2" in s1, s1
    s2 = textlayer.clean_residual_latex(r"风压: \left\\beginarrayl1 号:10 791pa \\2 号:11 193pa\endarray\right.")
    assert r"\begin" not in s2 and r"\end" not in s2 and r"\left" not in s2 and r"\right" not in s2, s2
    assert "1 号:10 791pa" in s2 and "2 号:11 193pa" in s2, s2

    # 2) Paper TOC detection: parenthesized page numbers + CIP copyright page protection
    fake_doc = {
        "pages": [
            {"page": 3, "w": 600, "h": 800, "blocks": [
                {"type": "title", "text": "图书在版编目(CIP)数据", "bbox": [50, 50, 300, 70]},
                {"type": "text", "text": "大连市志·物价志/大连市史志办公室编.—北京:方志出版社，2004.9", "bbox": [50, 80, 500, 100]},
                {"type": "text", "text": "ISBN 7-80192-314-6", "bbox": [50, 110, 300, 130]},
                {"type": "text", "text": "IV. K293.13", "bbox": [50, 140, 200, 160]},
                {"type": "text", "text": "1999年1月第一版", "bbox": [50, 170, 200, 190]},
            ]},
            {"page": 6, "w": 600, "h": 800, "blocks": [
                {"type": "title", "text": "目录", "bbox": [50, 50, 120, 75]},
                {"type": "title", "text": "第一章 机构沿革…… (30)", "bbox": [50, 100, 500, 120]},
                {"type": "title", "text": "第二章 税制税额……（44）", "bbox": [50, 130, 500, 150]},
                {"type": "title", "text": "第三章 税收管理…… (337)", "bbox": [50, 160, 500, 180]},
            ]},
            {"page": 7, "w": 600, "h": 800, "blocks": [
                {"type": "title", "text": "第二节 大连北部地区税收管理 …… (339)", "bbox": [50, 100, 500, 120]},
                {"type": "title", "text": "第三节 新中国成立后税收管理 …… (340)", "bbox": [50, 130, 500, 150]},
            ]},
        ]
    }
    toc_pages = assemble.detect_toc_pages(fake_doc)
    assert 3 not in toc_pages, f"CIP page wrongly classified as TOC: {toc_pages}"
    assert 6 in toc_pages and 7 in toc_pages, f"Parenthesized TOC pages missed: {toc_pages}"

    # 3) Copyright metadata extraction (书社/书局, em-dash ISBN, 第一版 date, compiler) + 编后记 guard
    cp_doc = {
        "pages": [
            {"page": 2, "w": 600, "h": 800, "blocks": [
                {"type": "text", "text": "日照市志\n山东省日照市史志编纂委员会 编\n齐鲁书社出版发行\n787×1092 毫米 16 开本 53 印张\n1994年11月北京第一版\nISBN 7—5333—0401—2", "bbox": [50, 100, 500, 400]}
            ]}
        ]
    }
    meta = assemble.extract_copyright_metadata(cp_doc, {2: "copyright_page"})
    assert meta["publisher"] == "齐鲁书社", meta
    assert meta["date"] == "1994-11", meta
    assert meta["isbn"] == "7533304012", meta
    assert "日照市史志编纂委员会" in meta["author"], meta

    postscript_vis = [
        {"type": "title", "text": "编后记"},
        {"type": "text", "text": "本志经多年编修，于1996年12月出版印刷，责任编辑付出辛劳。" * 10},
    ]
    assert not assemble.is_copyright_page_blocks(postscript_vis, "编后记\n" + postscript_vis[1]["text"])

    # 4) Catalog title cleaning & 注:① footnote matching
    assert assemble.clean_book_title("XFZ13205.双城县志") == "双城县志"
    assert assemble.clean_book_title("极简上海志") == "极简上海志"
    entries = assemble._split_footnote_entries("注:①HP 即马力，下同。")
    assert entries == [["①", "HP 即马力，下同。"]], entries
    print("[PASS] EPUB quality fixes (de-LaTeX, TOC/CIP guard, metadata, 编后记 guard, 注:① footnotes)")


def main():
    with tempfile.TemporaryDirectory(prefix="test_ocr_fixes_") as d:
        tmp = pathlib.Path(d)
        test_strip_old_layer_synthetic(tmp)
        test_strip_old_layer_real_books(tmp)
        test_build_pdf_tmp_cleanup(tmp)
        test_equation_inline_and_table_continuation(tmp)
        test_xhtml_table_and_epubcheck(tmp)
        test_recrop_multi_image_fallback(tmp)
        test_review_suspects_cangshan(tmp)
        test_epub_quality_fixes()
    print("\nALL REGRESSION TESTS PASSED!")


if __name__ == "__main__":
    main()
