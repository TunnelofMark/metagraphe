"""Convert oldtext.json (tool C) into the normalized doc.json (each stored line = one text block)."""
import sys
import json
import pathlib

src = pathlib.Path(sys.argv[1])
data = json.loads(src.read_text(encoding="utf-8"))
pages = [{"page": p["page"], "w": p["width"], "h": p["height"],
          "blocks": [{"type": "text", "level": None, "bbox": l["bbox"], "text": l["text"], "html": None,
                      "lines": [l], "image": None} for l in p["lines"]]} for p in data]
out = src.with_name("doc.json")
out.write_text(json.dumps({"tool": "oldtext", "pages": pages}, ensure_ascii=False), encoding="utf-8")
print(out)
