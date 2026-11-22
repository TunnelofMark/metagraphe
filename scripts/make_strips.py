"""Cut a page's native scan into readable strips (per column, top->bottom) for manual proofreading.

  python make_strips.py <pdf> <out_dir> <page> [<page> ...]   (cols/rows auto: 2 columns x 4 rows)
"""
import sys
import pathlib
import pymupdf

pdf, out = sys.argv[1], pathlib.Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
doc = pymupdf.open(pdf)
for pno in map(int, sys.argv[3:]):
    page = doc[pno - 1]
    r = page.rect
    cols, rows = 2, 4
    for c in range(cols):
        for k in range(rows):
            clip = pymupdf.Rect(r.x0 + r.width * c / cols - (6 if c else 0), r.y0 + r.height * k / rows - (8 if k else 0),
                                r.x0 + r.width * (c + 1) / cols + (6 if c == 0 else 0), r.y0 + r.height * (k + 1) / rows + 8)
            page.get_pixmap(dpi=300, clip=clip & r).save(out / f"p{pno:04d}_c{c + 1}_r{k + 1}.png")
    page.get_pixmap(dpi=110).save(out / f"p{pno:04d}_full.png")
    print(pno, "ok")
