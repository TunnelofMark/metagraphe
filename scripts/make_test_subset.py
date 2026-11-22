"""Build per-book test inputs without touching the source PDF.

For each book in books.json:
  ocr_test/source/<book>/test.pdf          subset of the test pages (objects copied, images not re-encoded)
  ocr_test/source/<book>/orig/pNNNN.<ext>  the embedded scan of each test page at native resolution
  ocr_test/source/<book>/preview/pNNNN.png ~1000px tall preview for visual checking
  ocr_test/source/<book>/pages.json        subset index -> original page number, image meta

Usage: python make_test_subset.py <books.json> <out_root> [book ...]
"""
import sys
import json
import pathlib
import pymupdf


def parse_ranges(s):
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def page_image(doc, page):
    imgs = page.get_images(full=True)
    if len(imgs) != 1:
        raise RuntimeError(f"page {page.number + 1}: expected 1 image, got {len(imgs)}")
    return imgs[0]


def extract_native(doc, xref, filt):
    """Return (bytes, ext). JPEG passes through untouched; JBIG2 is decoded to a 1-bit PNG."""
    if filt == "DCTDecode":
        return doc.xref_stream_raw(xref), "jpg"
    pix = pymupdf.Pixmap(doc, xref)
    return pix.tobytes("png"), "png"


def main():
    books = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_root = pathlib.Path(sys.argv[2])
    names = sys.argv[3:] or list(books)
    for name in names:
        cfg = books[name]
        pages = parse_ranges(cfg["test_pages"])
        src = pymupdf.open(cfg["pdf"])
        out = out_root / "source" / name
        (out / "orig").mkdir(parents=True, exist_ok=True)
        (out / "preview").mkdir(parents=True, exist_ok=True)

        sub = pymupdf.open()
        index = []
        for pno in pages:
            sub.insert_pdf(src, from_page=pno - 1, to_page=pno - 1)
            page = src[pno - 1]
            xref, _, w, h, bpc, cs, _, _, filt, *_ = page_image(src, page)
            data, ext = extract_native(src, xref, filt)
            (out / "orig" / f"p{pno:04d}.{ext}").write_bytes(data)
            scale = 1000 / page.rect.height
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace="rgb").save(out / "preview" / f"p{pno:04d}.png")
            bbox = page.get_image_bbox(page.get_images(full=True)[0])
            index.append({"sub_idx": len(index), "page": pno, "filter": filt, "w": w, "h": h, "bpc": bpc,
                          "cs": cs, "dpi": round(w / (bbox.width / 72)), "file": f"orig/p{pno:04d}.{ext}"})
        sub.save(out / "test.pdf", garbage=3, deflate=False)
        (out / "pages.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        print(name, len(pages), "pages ->", out / "test.pdf", round((out / "test.pdf").stat().st_size / 1e6, 1), "MB")


if __name__ == "__main__":
    main()
