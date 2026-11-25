#!/usr/bin/env python3
"""Marker-inspired suspect-page triage & visual review pack generator (read-only on book outputs).

Usage:
  Single book (writes review_queue.json + review_report.html + thumbs/ into --out):
    /opt/ocr/post/bin/python review_suspects.py <book_dir> [--out <review_dir>] [--max-thumbs 24]

  Batch triage summary across all books in ocr_full/ (writes a compact JSON summary):
    /opt/ocr/post/bin/python review_suspects.py --batch /mnt/e/project/metagraphe/ocr_full --out /tmp/batch_triage.json

Never modifies original PDFs or existing book deliverables. Never hallucinates or fabricates text.
"""
import argparse
import collections
import html
import json
import pathlib
import sys
import numpy as np
import pymupdf

from qa_book import CJK_EXT, LATEX_RAW, OK, PUBLISHING_OK, scan_ink_metrics


def jload(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def page_char_stats(blocks):
    text = "".join((b.get("text") or "") for b in blocks if b.get("type") != "image")
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return {"chars": 0, "latex": 0, "unknown": 0, "publishing": 0, "unknown_top": []}
    bad = [c for c in chars if not OK.match(c) and not CJK_EXT.match(c)]
    latex = [c for c in bad if c in LATEX_RAW]
    pub = [c for c in bad if c not in LATEX_RAW and PUBLISHING_OK.match(c)]
    unk = [c for c in bad if c not in LATEX_RAW and not PUBLISHING_OK.match(c)]
    return {
        "chars": len(chars),
        "latex": len(latex),
        "unknown": len(unk),
        "publishing": len(pub),
        "unknown_top": collections.Counter(unk).most_common(10),
        "sample": text[:240],
    }


def triage_book(book_dir, pdf_override=None, top_k=15):
    book_dir = pathlib.Path(book_dir)
    name = book_dir.name
    qa = jload(book_dir / "qa.json", {})
    bj = jload(book_dir / "book.json", {"pages": []})
    hl = jload(book_dir / "hybrid_log.json", [])
    bp = jload(book_dir / "logs" / "build_pdf.json", {})

    pdf_path = None
    for cand in (pdf_override, book_dir / f"{name}_双层.pdf", qa.get("source_pdf")):
        if cand and pathlib.Path(cand).exists():
            pdf_path = pathlib.Path(cand)
            break

    pages_by_no = {p["page"]: p for p in bj.get("pages", [])}
    queue = {
        "book": name,
        "book_dir": str(book_dir),
        "pdf_inspected": str(pdf_path) if pdf_path else None,
        "qa_status": qa.get("status"),
        "qa_reasons": qa.get("reasons", []),
        "missed_content_blank": [],
        "margin_only_blank": [],
        "high_latex_pages": [],
        "high_unknown_char_pages": [],
        "major_hybrid_edits": [],
        "preserved_visible_text_pages": bp.get("preserved_visible_text_pages") or qa.get("preserved_visible_text_pages") or [],
        "duplicate_overlap_pages": qa.get("duplicate_overlap_pages", []),
    }

    # 1) Blank page triage (distinguish outer scan borders from genuine interior ink)
    blank_detail = qa.get("blank_pages_detail", [])
    doc = pymupdf.open(str(pdf_path)) if pdf_path else None
    try:
        for item in blank_detail:
            pno = item["page"]
            full_ink = item.get("scan_ink_share", 0.0)
            inner10 = item.get("interior_ink_share")
            inner5 = None
            if doc is not None and 1 <= pno <= doc.page_count:
                if inner10 is None:
                    full_ink, inner10 = scan_ink_metrics(doc[pno - 1], margin_frac=0.10)
                _, inner5 = scan_ink_metrics(doc[pno - 1], margin_frac=0.05)
            rec = {
                "page": pno,
                "scan_ink_share": full_ink,
                "interior_10pct_ink": inner10,
                "interior_5pct_ink": inner5,
            }
            if (inner10 is not None and inner10 >= 0.002) or (inner5 is not None and inner5 >= 0.005):
                rec["priority"] = "high"
                rec["reason"] = "空白识别页但内芯区域存在墨水，疑似双模型漏识别"
                queue["missed_content_blank"].append(rec)
            elif full_ink >= 0.002:
                rec["priority"] = "low"
                rec["reason"] = "仅外沿 10% 边距有扫描黑边/装订影，内芯为空白"
                queue["margin_only_blank"].append(rec)
    finally:
        if doc is not None:
            doc.close()

    # 2) Per-page LaTeX residue & unknown non-CJK triage
    latex_hits, unk_hits = [], []
    for pno, p in pages_by_no.items():
        st = page_char_stats(p.get("blocks", []))
        if st["chars"] < 10:
            continue
        lr = st["latex"] / st["chars"]
        ur = st["unknown"] / st["chars"]
        if st["latex"] >= 8 and lr >= 0.01:
            latex_hits.append({
                "page": pno,
                "latex_chars": st["latex"],
                "latex_ratio": round(lr, 4),
                "sample": st["sample"],
                "priority": "medium",
                "reason": f"页内含 {st['latex']} 个疑似未清洗 LaTeX 字符（占比 {lr:.2%}）",
            })
        if st["unknown"] >= 5 and ur >= 0.01:
            unk_hits.append({
                "page": pno,
                "unknown_chars": st["unknown"],
                "unknown_ratio": round(ur, 4),
                "unknown_top": st["unknown_top"],
                "sample": st["sample"],
                "priority": "high",
                "reason": f"页内含 {st['unknown']} 个未知异常字符（占比 {ur:.2%}）",
            })
    queue["high_latex_pages"] = sorted(latex_hits, key=lambda x: -x["latex_chars"])[:top_k]
    queue["high_unknown_char_pages"] = sorted(unk_hits, key=lambda x: -x["unknown_chars"])[:top_k]

    # 3) Major PP-DocLayoutV3 image reclassifications (guard against body text eaten into images)
    major_hyb = []
    for h in hl:
        removed = h.get("removed_text") or []
        total_len = sum(len(t) for _, t in removed)
        if len(removed) >= 3 or total_len >= 25:
            major_hyb.append({
                "page": h["page"],
                "action": h["action"],
                "removed_count": len(removed),
                "removed_preview": removed[:5],
                "layout_score": h.get("layout_score"),
                "priority": "medium",
                "reason": f"PP-DocLayoutV3 将 {len(removed)} 个文本/标题块改判为图片，建议确认无正文误吞",
            })
    queue["major_hybrid_edits"] = sorted(major_hyb, key=lambda x: -x["removed_count"])[:top_k]

    # 4) Suggested targeted re-check command templates (not executed)
    high_pages = sorted({
        *(x["page"] for x in queue["missed_content_blank"]),
        *(x["page"] for x in queue["high_unknown_char_pages"][:5]),
        *(x["page"] for x in queue["preserved_visible_text_pages"][:5]),
    })
    queue["high_priority_pages"] = high_pages
    if high_pages and qa.get("source_pdf"):
        prange = ",".join(str(p) for p in high_pages[:10])
        queue["suggested_verify_cmd"] = (
            f"/opt/ocr/post/bin/python {pathlib.Path(__file__).resolve().parent / 'verify_pdf.py'} "
            f"\"{book_dir / f'{name}_双层.pdf'}\" \"{qa['source_pdf']}\" "
            f"--pages {prange} --show {prange} --png-dir /tmp/verify_{name}"
        )
    return queue


def render_review_html(queue, out_dir, max_thumbs=24):
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(exist_ok=True)

    cards = []
    # Collect prioritized pages to render
    items = []
    for x in queue["missed_content_blank"]:
        items.append(("高优·疑似漏识空白页", x["page"], x["reason"], json.dumps(x, ensure_ascii=False)))
    for x in queue["high_unknown_char_pages"]:
        items.append(("高优·未知异常字符", x["page"], x["reason"], x.get("sample", "")))
    for pno in queue["preserved_visible_text_pages"]:
        items.append(("中优·保留可见矢量字", pno, "build_pdf 检测到可见矢量文字并原样保留，请核对非旧层残留", ""))
    for x in queue["high_latex_pages"]:
        items.append(("中优·LaTeX 残留", x["page"], x["reason"], x.get("sample", "")))
    for x in queue["major_hybrid_edits"]:
        items.append(("中优·大图区改判", x["page"], x["reason"], json.dumps(x["removed_preview"], ensure_ascii=False)))
    # Include up to 3 margin-only blank samples for quick visual confirmation
    for x in queue["margin_only_blank"][:3]:
        items.append(("抽样·仅外沿黑边空白页", x["page"], x["reason"], json.dumps(x, ensure_ascii=False)))

    pdf_path = queue.get("pdf_inspected")
    doc = pymupdf.open(pdf_path) if pdf_path and pathlib.Path(pdf_path).exists() else None
    rendered = set()
    try:
        for idx, (tag, pno, reason, detail) in enumerate(items[:max_thumbs]):
            img_tag = ""
            if doc is not None and 1 <= pno <= doc.page_count:
                fname = f"p{pno:04d}.png"
                if fname not in rendered:
                    pix = doc[pno - 1].get_pixmap(dpi=72, colorspace=pymupdf.csRGB)
                    pix.save(str(thumbs_dir / fname))
                    rendered.add(fname)
                img_tag = f'<img src="thumbs/{fname}" loading="lazy" alt="p{pno}">'
            cards.append(
                f'<div class="card"><div class="meta"><b>[{html.escape(tag)}] 第 {pno} 页</b> — '
                f'{html.escape(reason)}</div><div class="body">{img_tag}'
                f'<pre>{html.escape(detail[:500])}</pre></div></div>'
            )
    finally:
        if doc is not None:
            doc.close()

    summary_html = (
        f"<ul>"
        f"<li>疑似漏识空白页（内芯有墨水）：<b>{len(queue['missed_content_blank'])}</b></li>"
        f"<li>仅外沿黑边空白页（内芯空白）：<b>{len(queue['margin_only_blank'])}</b></li>"
        f"<li>未知异常字符高发页：<b>{len(queue['high_unknown_char_pages'])}</b></li>"
        f"<li>LaTeX 残留高发页：<b>{len(queue['high_latex_pages'])}</b></li>"
        f"<li>PP-DocLayoutV3 大幅改判页：<b>{len(queue['major_hybrid_edits'])}</b></li>"
        f"<li>保留可见矢量文字页：<b>{len(queue['preserved_visible_text_pages'])}</b></li>"
        f"</ul>"
    )
    page_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>疑难页复核看板 - {html.escape(queue['book'])}</title>
<style>
body{{font-family:system-ui,"Noto Sans CJK SC",sans-serif;max-width:1100px;margin:20px auto;padding:0 16px;background:#faf9f6;color:#222}}
.card{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;margin:14px 0}}
.meta{{margin-bottom:8px;font-size:14px}}
.body{{display:grid;grid-template-columns:280px 1fr;gap:14px;align-items:start}}
img{{max-width:100%;border:1px solid #ccc}}
pre{{white-space:pre-wrap;word-break:break-all;background:#f5f4f0;padding:8px;font-size:12px;margin:0}}
</style></head><body>
<h1>疑难页复核看板：{html.escape(queue['book'])}</h1>
<p>当前 QA 状态：<b>{html.escape(str(queue['qa_status']))}</b> | 原因：{html.escape('；'.join(queue['qa_reasons']) or '无')}</p>
{summary_html}
{''.join(cards) if cards else '<p>未检出需人工复核的疑难页。</p>'}
</body></html>"""
    (out_dir / "review_queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "review_report.html").write_text(page_html, encoding="utf-8")
    return out_dir / "review_queue.json", out_dir / "review_report.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book_dir", nargs="?", help="Single book output directory under ocr_full/")
    ap.add_argument("--batch", help="Scan all book directories under this root (e.g. ocr_full)")
    ap.add_argument("--out", help="Output directory (single book) or JSON file (--batch)")
    ap.add_argument("--pdf", help="Optional override PDF path for single-book inspection")
    ap.add_argument("--max-thumbs", type=int, default=24)
    a = ap.parse_args()

    if a.batch:
        root = pathlib.Path(a.batch)
        rows = []
        for qa_f in sorted(root.glob("*/qa.json")):
            q = triage_book(qa_f.parent)
            rows.append({
                "book": q["book"],
                "qa_status": q["qa_status"],
                "missed_content_blank": len(q["missed_content_blank"]),
                "margin_only_blank": len(q["margin_only_blank"]),
                "high_unknown_char_pages": len(q["high_unknown_char_pages"]),
                "high_latex_pages": len(q["high_latex_pages"]),
                "major_hybrid_edits": len(q["major_hybrid_edits"]),
                "preserved_visible_text_pages": len(q["preserved_visible_text_pages"]),
            })
        summary = {
            "total_books": len(rows),
            "books_with_missed_content_blank": [r["book"] for r in rows if r["missed_content_blank"]],
            "books_with_margin_only_blank": [r["book"] for r in rows if r["margin_only_blank"]],
            "books_with_high_unknown_chars": [r["book"] for r in rows if r["high_unknown_char_pages"]],
            "books_with_high_latex": [r["book"] for r in rows if r["high_latex_pages"]],
            "books": rows,
        }
        out_p = pathlib.Path(a.out) if a.out else (root / "_batch" / "triage_summary.json")
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in summary.items() if k != "books"}, ensure_ascii=False, indent=1))
        return

    if not a.book_dir:
        ap.error("Provide <book_dir> or --batch <ocr_full_dir>")
    q = triage_book(a.book_dir, pdf_override=a.pdf)
    out_dir = pathlib.Path(a.out) if a.out else (pathlib.Path(a.book_dir) / "review")
    qj, rh = render_review_html(q, out_dir, max_thumbs=a.max_thumbs)
    print(json.dumps({
        "book": q["book"],
        "queue_json": str(qj),
        "report_html": str(rh),
        "missed_content_blank": len(q["missed_content_blank"]),
        "margin_only_blank": len(q["margin_only_blank"]),
        "high_latex_pages": len(q["high_latex_pages"]),
        "high_unknown_char_pages": len(q["high_unknown_char_pages"]),
        "major_hybrid_edits": len(q["major_hybrid_edits"]),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
