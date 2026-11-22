"""Check the text layer with pdfium (the engine of Chrome / Edge): extraction, search, char boxes.
  python verify_pdfium.py <pdf> <page_index0> <term> [<term> ...]"""
import sys, json
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument(sys.argv[1])
res = {"pages": len(pdf), "search": {}}
for term in sys.argv[3:]:
    hits = []
    for i in range(len(pdf)):
        tp = pdf[i].get_textpage()
        s = tp.search(term)
        if s.get_next():
            hits.append(i)
    res["search"][term] = hits
pg = pdf[int(sys.argv[2])]
tp = pg.get_textpage()
txt = tp.get_text_range()
res["sample_text"] = txt[:120]
res["n_chars"] = tp.count_chars()
res["first_char_boxes"] = [tuple(round(v, 1) for v in tp.get_charbox(i)) for i in range(3)]
print(json.dumps(res, ensure_ascii=False, indent=1))
