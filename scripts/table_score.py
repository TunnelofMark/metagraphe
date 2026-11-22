"""Cell accuracy on the hand-verified tables (verified grids saved in eval/tables_verified.json).

  python table_score.py save      # write the verified grids (MinerU's grids, checked cell by cell against the scans)
  python table_score.py score     # score every tool against them
"""
import sys
import json
import pathlib
from eval_tables import grid, tables, ROOT

VERIFIED = [("xigang", 102, None), ("xigang", 301, None), ("xigang", 302, None),
            ("rizhao", 412, None), ("rizhao", 264, 13)]   # (book, page, rows checked: None = all)
OUT = ROOT / "eval" / "tables_verified.json"


def main():
    if sys.argv[1] == "save":
        data = {f"{b}:{p}": grid(tables("mineru_advanced", b, p)[0]["html"])[:n] for b, p, n in VERIFIED}
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        print(OUT, {k: len(v) for k, v in data.items()})
        return
    ref = json.loads(OUT.read_text(encoding="utf-8"))
    res = {}
    for tool in ("mineru_advanced", "paddleocr_vl", "ppstructurev3"):
        tot = ok = struct_ok = 0
        per = {}
        for key, g in ref.items():
            b, p = key.split(":"); tb = tables(tool, b, int(p))
            h = grid(tb[0]["html"]) if tb else []
            same_shape = len(h) >= len(g) and all(len(h[r]) == len(g[r]) for r in range(len(g)))
            n = sum(len(r) for r in g)
            good = sum(1 for r in range(len(g)) for c in range(len(g[r]))
                       if r < len(h) and c < len(h[r]) and h[r][c] == g[r][c])
            per[key] = {"cells": n, "correct": good, "same_grid": same_shape}
            tot += n; ok += good; struct_ok += same_shape
        res[tool] = {"cells": tot, "correct": ok, "acc_%": round(100 * ok / tot, 2), "grids_same": f"{struct_ok}/{len(ref)}", "per_table": per}
        print(tool, json.dumps({k: v for k, v in res[tool].items() if k != "per_table"}, ensure_ascii=False),
              {k: f"{v['correct']}/{v['cells']}{'' if v['same_grid'] else ' 结构不同'}" for k, v in per.items()})
    (ROOT / "eval" / "table_scores.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
