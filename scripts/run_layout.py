"""Stand-alone PP-DocLayoutV3 layout detection (for hybrid_images.py), same json shape as PaddleOCR-VL.

  /opt/ocr/paddle/bin/python run_layout.py <pdf> <pages.json> <out_dir> [--dpi 144]
Writes <out_dir>/pNNNN_res.json = {"width", "height", "layout_det_res": {"boxes": [...]}} and timing.json.
"""
import sys
import json
import time
import argparse
import pathlib
import numpy as np
import pymupdf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("pages"); ap.add_argument("out")
    ap.add_argument("--dpi", type=int, default=144)
    a = ap.parse_args()
    from paddleocr import LayoutDetection
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    meta = json.loads(pathlib.Path(a.pages).read_text(encoding="utf-8"))
    t0 = time.time()
    model = LayoutDetection(model_name="PP-DocLayoutV3")
    t_load = time.time() - t0
    doc = pymupdf.open(a.pdf)
    t1 = time.time()
    for m in meta:
        pix = doc[m["sub_idx"]].get_pixmap(dpi=a.dpi, colorspace=pymupdf.csRGB)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
        res = next(iter(model.predict(img, batch_size=1)))
        boxes = [{"label": b["label"], "score": float(b["score"]), "coordinate": [float(v) for v in b["coordinate"]]}
                 for b in res["boxes"]]
        (out / f"p{m['page']:04d}_res.json").write_text(json.dumps(
            {"width": pix.width, "height": pix.height, "layout_det_res": {"boxes": boxes}}), encoding="utf-8")
    el = time.time() - t1
    (out / "timing.json").write_text(json.dumps({"model_load_s": round(t_load, 1), "pages": len(meta),
                                                 "seconds": round(el, 1), "sec_per_page": round(el / len(meta), 3)}))
    print("layout", len(meta), "pages", round(el, 1), "s")


if __name__ == "__main__":
    main()
