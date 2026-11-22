"""Compare tables across tools: grid shape (after expanding rowspan/colspan) and cell-by-cell content.

  python eval_tables.py <book> <page> [<page> ...] --tools mineru_advanced paddleocr_vl ppstructurev3
Prints, per page and table: rows x cols per tool, header span count, and every cell where the tools disagree
(cells are matched by grid position). Use the printout to check the disputed cells against the scan.
"""
import re
import json
import argparse
import pathlib
import unicodedata
from lxml import html as lhtml

ROOT = pathlib.Path(__file__).resolve().parent.parent / "ocr_test"


def grid(html):
    """Expand an HTML table into a 2-D list of cell strings (spanned cells repeat their text)."""
    if not html:
        return []
    t = lhtml.fragment_fromstring(html, create_parent="div").find(".//table")
    if t is None:
        return []
    out, pending = [], {}
    for r, tr in enumerate(t.iter("tr")):
        row, c = [], 0
        cells = list(tr.iter("td", "th"))
        k = 0
        while k < len(cells) or (r, c) in pending:
            if (r, c) in pending:
                row.append(pending.pop((r, c))); c += 1; continue
            td = cells[k]; k += 1
            txt = unicodedata.normalize("NFKC", re.sub(r"\s+", "", td.text_content()))
            rs, cs = int(td.get("rowspan", 1) or 1), int(td.get("colspan", 1) or 1)
            for dc in range(cs):
                row.append(txt)
                for dr in range(1, rs):
                    pending[(r + dr, c + dc)] = txt
            c += cs
        out.append(row)
    return out


def tables(tool, book, page):
    doc = json.loads((ROOT / tool / book / "doc.json").read_text(encoding="utf-8"))
    for p in doc["pages"]:
        if p["page"] == page:
            return [b for b in p["blocks"] if b["type"] == "table"]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book"); ap.add_argument("pages", nargs="+", type=int)
    ap.add_argument("--tools", nargs="+", default=["mineru_advanced", "paddleocr_vl", "ppstructurev3"])
    a = ap.parse_args()
    for page in a.pages:
        tbs = {t: tables(t, a.book, page) for t in a.tools}
        n = max(len(v) for v in tbs.values())
        for i in range(n):
            print(f"=== {a.book} p{page} table {i + 1}")
            grids = {}
            for t, v in tbs.items():
                if i < len(v):
                    g = grid(v[i].get("html"))
                    grids[t] = g
                    spans = len(re.findall(r"(row|col)span", v[i].get("html") or ""))
                    print(f"  {t:18s} rows={len(g)} cols={max((len(r) for r in g), default=0)} spans={spans} "
                          f"caption={(v[i].get('caption') or '')[:30]!r}")
                else:
                    print(f"  {t:18s} (no table)")
            if len(grids) < 2:
                continue
            R = max(len(g) for g in grids.values())
            C = max((len(r) for g in grids.values() for r in g), default=0)
            diffs = 0
            for r in range(R):
                for c in range(C):
                    vals = {t: (g[r][c] if r < len(g) and c < len(g[r]) else "∅") for t, g in grids.items()}
                    if len(set(vals.values())) > 1:
                        diffs += 1
                        if diffs <= 40:
                            print(f"    r{r} c{c}: " + " | ".join(f"{t[:6]}={v}" for t, v in vals.items()))
            print(f"  cells differing: {diffs} of {R * C}")


if __name__ == "__main__":
    main()
