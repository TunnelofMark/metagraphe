"""Character-level comparison of tool output against a hand-checked reference transcription.

  python eval_text.py <refs_dir> <tool_name>=<doc.json> [...] --out report.json

refs_dir/pNNNN.txt : reference body text of original page NNNN (headers/page numbers excluded),
                     verified line by line against the scan image.
Counts per page: substitutions (错字), deletions (漏字), insertions (多字) after removing whitespace
and folding full/half-width forms (NFKC). Punctuation is scored separately from 汉字/数字/字母.
"""
import re
import sys
import json
import argparse
import pathlib
import unicodedata

BODY_SKIP = {"header", "footer", "page_number", "aside", "image", "table"}   # tables are scored cell by cell separately
PUNCT = re.compile(r"[\W_]", re.U)


def norm(s):
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("～", "~").replace("—", "-").replace("－", "-")
    return re.sub(r"\s+", "", s)


def page_text(doc, pno, include_tables=False):
    skip = BODY_SKIP - {"table"} if include_tables else BODY_SKIP
    for p in doc["pages"]:
        if p["page"] == pno:
            return "".join((b.get("text") or "") for b in p["blocks"]
                           if b["type"] not in skip and not (b["type"] == "caption" and b.get("parent")))
    return ""


def align(ref, hyp):
    """Levenshtein alignment; returns list of (op, ref_char, hyp_char, ref_pos)."""
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    back = [bytearray(m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        back[0][j] = 2
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        back[i][0] = 1
        r = ref[i - 1]
        for j in range(1, m + 1):
            sub = prev[j - 1] + (r != hyp[j - 1])
            dele = prev[j] + 1
            ins = cur[j - 1] + 1
            best = min(sub, dele, ins)
            cur[j] = best
            back[i][j] = 0 if best == sub else (1 if best == dele else 2)
        prev = cur
    ops, i, j = [], n, m
    while i > 0 or j > 0:
        b = back[i][j]
        if i > 0 and j > 0 and b == 0:
            if ref[i - 1] != hyp[j - 1]:
                ops.append(("S", ref[i - 1], hyp[j - 1], i - 1))
            i, j = i - 1, j - 1
        elif i > 0 and (j == 0 or b == 1):
            ops.append(("D", ref[i - 1], "", i - 1)); i -= 1
        else:
            ops.append(("I", "", hyp[j - 1], i)); j -= 1
    return ops[::-1]


def score(ref, hyp):
    ops = align(ref, hyp)
    res = {"ref_chars": len(ref), "S": 0, "D": 0, "I": 0, "punct_errors": 0, "digit_errors": 0, "errors": []}
    for op, r, h, pos in ops:
        ch = r or h
        if PUNCT.match(ch) and (not r or PUNCT.match(r)) and (not h or PUNCT.match(h)):
            res["punct_errors"] += 1
            continue
        res[op] += 1
        if any(c.isdigit() for c in r + h):
            res["digit_errors"] += 1
        res["errors"].append({"op": op, "ref": r, "hyp": h, "ctx": ref[max(0, pos - 8):pos + 8]})
    core = len(PUNCT.sub("", ref))
    res["core_chars"] = core
    res["CER_core_%"] = round(100 * (res["S"] + res["D"] + res["I"]) / max(1, core), 3)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("refs"); ap.add_argument("tools", nargs="+"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    refs = sorted(pathlib.Path(a.refs).glob("p*.txt"))
    out = {}
    for spec in a.tools:
        name, path = spec.split("=", 1)
        doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        out[name] = {}
        for rf in refs:
            pno = int(rf.stem[1:])
            # a pNNNN.tables marker means the page's list content may legitimately come out as a table
            inc = rf.with_suffix(".tables").exists()
            out[name][pno] = score(norm(rf.read_text(encoding="utf-8")), norm(page_text(doc, pno, inc)))
        tot = {k: sum(v[k] for v in out[name].values()) for k in ("ref_chars", "core_chars", "S", "D", "I", "punct_errors", "digit_errors")}
        tot["CER_core_%"] = round(100 * (tot["S"] + tot["D"] + tot["I"]) / max(1, tot["core_chars"]), 3)
        out[name]["total"] = tot
        print(name, json.dumps(tot, ensure_ascii=False))
    pathlib.Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
