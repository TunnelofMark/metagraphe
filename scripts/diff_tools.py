"""List disagreements between two tools' page text (to guide proofreading against the scan).
  python diff_tools.py <docA.json> <docB.json> <page> [<page>...]"""
import sys, json, pathlib
from eval_text import norm, page_text, align
A = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
B = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
for pno in map(int, sys.argv[3:]):
    a, b = norm(page_text(A, pno)), norm(page_text(B, pno))
    ops = align(a, b)
    print(f"== p{pno}  A={len(a)} B={len(b)} diffs={len(ops)}")
    for op, r, h, pos in ops[:60]:
        print(f"  {op} A[{r}] B[{h}]  …{a[max(0,pos-10):pos]}【{r or '+'+h}】{a[pos+1:pos+8] if r else a[pos:pos+7]}")
