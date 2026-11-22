"""Inspect a scanned PDF: metadata, outline, text layer, embedded images per page.

Usage: python inspect_pdf.py <pdf> [pages like 1-12,99-110]
"""
import sys
import json
import collections
import pymupdf


def parse_ranges(s, n):
    if not s:
        return list(range(1, n + 1))
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def main():
    path = sys.argv[1]
    doc = pymupdf.open(path)
    print("file:", path)
    print("pages:", doc.page_count, "| pdf version:", doc.metadata.get("format"))
    print("metadata:", json.dumps(doc.metadata, ensure_ascii=False))
    toc = doc.get_toc()
    print("outline entries:", len(toc))
    for lvl, title, pno in toc[:40]:
        print("  " * lvl, title, "->", pno)

    # whole-book survey
    kinds = collections.Counter()
    text_pages = 0
    sizes = collections.Counter()
    for page in doc:
        imgs = page.get_images(full=True)
        for img in imgs:
            xref, smask, w, h, bpc, cs, alt, name, filt, *_ = img
            kinds[(filt, cs, bpc)] += 1
        if page.get_text("text").strip():
            text_pages += 1
        r = page.rect
        sizes[(round(r.width), round(r.height))] += 1
    print("pages with text layer:", text_pages)
    print("page sizes (pt):", sizes.most_common(5))
    print("image kinds (filter, colorspace, bpc):", kinds.most_common())

    pages = parse_ranges(sys.argv[2] if len(sys.argv) > 2 else "", doc.page_count)
    print("\n--- per page detail ---")
    for pno in pages:
        page = doc[pno - 1]
        r = page.rect
        infos = []
        for img in page.get_images(full=True):
            xref, smask, w, h, bpc, cs, alt, name, filt, *_ = img
            bbox = page.get_image_bbox(img)
            dpi = round(w / (bbox.width / 72)) if bbox.width else 0
            infos.append(f"{filt}/{cs}/{bpc}bit {w}x{h} ~{dpi}dpi")
        txt = page.get_text("text").strip().replace("\n", " ")
        fonts = {f[3] for f in page.get_fonts()}
        print(f"p{pno} {round(r.width)}x{round(r.height)} rot={page.rotation} imgs={infos} "
              f"fonts={sorted(fonts)[:3]} text[{len(txt)}]={txt[:60]!r}")


if __name__ == "__main__":
    main()
