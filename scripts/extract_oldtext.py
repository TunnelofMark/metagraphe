"""Tool C: dump the PDF's existing (超星) hidden text layer for the test pages as a baseline.

Outputs in <out_dir>:
  oldtext.md        page-by-page plain text (reading order as stored in the PDF)
  oldtext.json      per page: lines with bbox (PDF points, unrotated page space) and text
Usage: python extract_oldtext.py <pdf> <pages like 1-12,99-110> <out_dir>
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


def main():
    pdf, ranges, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    md, js = [], []
    for pno in parse_ranges(ranges):
        page = doc[pno - 1]
        d = page.get_text("dict")
        lines = []
        for b in d["blocks"]:
            for l in b.get("lines", []):
                t = "".join(s["text"] for s in l["spans"]).strip()
                if t:
                    lines.append({"bbox": [round(v, 1) for v in l["bbox"]], "text": t})
        js.append({"page": pno, "width": page.rect.width, "height": page.rect.height,
                   "rotation": page.rotation, "lines": lines})
        md.append(f"<!-- page {pno} -->\n" + page.get_text("text").strip() + "\n")
    (out / "oldtext.md").write_text("\n".join(md), encoding="utf-8")
    (out / "oldtext.json").write_text(json.dumps(js, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out, len(js), "pages")


if __name__ == "__main__":
    main()
