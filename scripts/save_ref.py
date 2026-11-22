"""Dump one page's body text from a doc.json as a reference draft (after visual verification).
  python save_ref.py <doc.json> <page> <out.txt>"""
import sys, json, pathlib
from eval_text import BODY_SKIP
doc = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
pno = int(sys.argv[2])
p = next(p for p in doc["pages"] if p["page"] == pno)
txt = "\n".join(b["text"] for b in p["blocks"] if b["type"] not in BODY_SKIP and b.get("text") and not (b["type"] == "caption" and b.get("parent")))
pathlib.Path(sys.argv[3]).write_bytes((txt + "\n").encode("utf-8"))
print(len(txt), "chars")
