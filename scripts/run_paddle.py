"""Tool B: PaddleOCR-VL 1.6 or PP-StructureV3 on a book's test subset.

  python run_paddle.py vl  <book> [--server http://127.0.0.1:8118/v1]
  python run_paddle.py ppsv3 <book>

Outputs: <repo>/ocr_test/{paddleocr_vl,ppstructurev3}/<book>/raw/
  pNNNN_res.json / .md per page (+ imgs/), merged/ (restructure_pages: cross-page tables, title levels)
  timing.json : per-page wall time measured around predict()
"""
import sys
import json
import time
import pathlib
import argparse

P = pathlib.Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", choices=["vl", "ppsv3"])
    ap.add_argument("book")
    ap.add_argument("--server", default=None, help="vLLM server URL for PaddleOCR-VL")
    a = ap.parse_args()

    src = P / "ocr_test/source" / a.book / "test.pdf"
    pages = json.loads((P / "ocr_test/source" / a.book / "pages.json").read_text())
    out = P / "ocr_test" / ("paddleocr_vl" if a.tool == "vl" else "ppstructurev3") / a.book / "raw"
    out.mkdir(parents=True, exist_ok=True)

    t_load = time.time()
    if a.tool == "vl":
        from paddleocr import PaddleOCRVL
        kw = dict(pipeline_version="v1.6")
        if a.server:
            kw.update(vl_rec_backend="vllm-server", vl_rec_server_url=a.server)
        pipe = PaddleOCRVL(**kw)
    else:
        from paddleocr import PPStructureV3
        pipe = PPStructureV3(lang="ch", use_doc_orientation_classify=False, use_doc_unwarping=False,
                             use_formula_recognition=False, use_chart_recognition=False, use_seal_recognition=False)
    t_load = time.time() - t_load

    results, per_page = [], []
    t0 = time.time()
    last = t0
    for i, res in enumerate(pipe.predict(str(src))):
        now = time.time()
        per_page.append(round(now - last, 2)); last = now
        pno = pages[i]["page"]
        res.save_to_json(save_path=str(out / f"p{pno:04d}_res.json"))
        res.save_to_markdown(save_path=str(out / f"p{pno:04d}"))
        results.append(res)
    total = time.time() - t0

    (out.parent / "timing.json").write_text(json.dumps({
        "model_load_s": round(t_load, 1), "predict_total_s": round(total, 1), "pages": len(per_page),
        "sec_per_page": round(total / max(1, len(per_page)), 2), "per_page": per_page}, indent=1))
    merged = out / "merged"
    merged.mkdir(exist_ok=True)
    if hasattr(pipe, "restructure_pages"):
        for r in pipe.restructure_pages(results, merge_tables=True, relevel_titles=True, concatenate_pages=True):
            r.save_to_markdown(save_path=str(merged))
            r.save_to_json(save_path=str(merged / "merged_res.json"))
    elif hasattr(pipe, "concatenate_markdown_pages"):
        md = pipe.concatenate_markdown_pages([r.markdown for r in results])
        if isinstance(md, dict):
            md = md.get("markdown_texts", "")
        (merged / "merged.md").write_text(md if isinstance(md, str) else "\n".join(md), encoding="utf-8")

    print("done", a.tool, a.book, len(per_page), "pages", round(total, 1), "s")


if __name__ == "__main__":
    main()
