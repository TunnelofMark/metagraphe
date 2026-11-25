#!/usr/bin/env python3
"""Batch driver for the 地方志 full-book OCR (runs inside WSL, stdlib only).

  python3 scripts/run_all.py [--only NAME ...] [--limit N] [--root DIR]

  Start / resume (from PowerShell):
    $env:WSL_UTF8=1; wsl -d Ubuntu-24.04 -u root -- bash /mnt/e/project/metagraphe/scripts/run_all.sh [args]

Per book (name = file stem, e.g. XFZ23703.西岗区志) everything goes to ocr_full/<name>/:
  book.md images/ book.json <name>.epub <name>_双层.pdf qa.json hybrid_log.json logs/ state/ work/
  work/ keeps pages.json, result.zip, raw/, layout/ (PP-DocLayoutV3 jsons), doc.json, doc_hybrid.json.

Stages: recognize (MinerU, GPU) -> layout (PP-DocLayoutV3, GPU) -> post (CPU) -> qa (CPU).
- GPU stages run strictly one at a time, book after book.
- post+qa of a book run in a pool of at most 2 workers, overlapping the next book's GPU stages.
- state/<stage>.json = {"done": true, "seconds", "started", "finished", "attempts"}; done stages are
  skipped on restart (resume = just start again).
- a failed stage is retried once; a second failure goes to ocr_full/_batch/failed.json and the book is
  skipped (later stages not run); the batch continues.
- before every book: free space on E: < 30 GB -> wait for running post jobs, write _batch/PAUSED.json, exit 3.
- qa marks books pass / needs_review; needs_review books are NOT re-run.
- _batch/progress.json is rewritten after each finished book; _batch/progress_report.txt every 10 books.
The source PDFs under H: are only ever read.
"""
import os
import sys
import json
import time
import shutil
import signal
import argparse
import datetime
import threading
import subprocess
import pathlib
from concurrent.futures import ThreadPoolExecutor

P = pathlib.Path(__file__).resolve().parent.parent
S = P / "scripts"
SRC_ROOT = pathlib.Path("/mnt/h/电子书/地方志")
OUT_ROOT = P / "ocr_full"
BATCH = OUT_ROOT / "_batch"
POST_PY = "/opt/ocr/post/bin/python"
MIN_FREE_GB = 30
LOCK = threading.Lock()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with LOCK:
        with open(BATCH / "run_all.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def jload(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def jdump(p, obj):
    p = pathlib.Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


class Book:
    def __init__(self, pdf):
        self.pdf = pdf
        self.name = pdf.stem                                  # XFZ23703.西岗区志
        # EPUB title = file stem (keeps the XFZ number). A bare "西岗区志" collides with the cover heading
        # of the same text: pandoc gives both the same id -> epubcheck RSC-005 "Duplicate ID".
        self.title = self.name
        self.dir = OUT_ROOT / self.name
        self.work = self.dir / "work"
        self.logs = self.dir / "logs"
        self.state = self.dir / "state"
        for d in (self.work, self.logs, self.state):
            d.mkdir(parents=True, exist_ok=True)

    def done(self, stage):
        return bool((jload(self.state / f"{stage}.json") or {}).get("done"))

    def pages(self):
        pj = jload(self.work / "pages.json")
        return len(pj) if pj else None


def run_cmd(book, stage, cmd, timeout=None):
    """Run in its own process group so a timeout kills MinerU's / vLLM's children too (frees the GPU)."""
    logf = book.logs / f"{stage}.log"
    with open(logf, "a", encoding="utf-8") as f:
        f.write(f"\n===== {now()} {' '.join(map(str, cmd))}\n")
        f.flush()
        pr = subprocess.Popen([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = pr.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(pr.pid, signal.SIGTERM)
            time.sleep(20)
            try:
                os.killpg(pr.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            pr.wait()
            raise RuntimeError(f"{stage}: timeout after {timeout}s, process group killed, see {logf}")
    if rc != 0:
        raise RuntimeError(f"{stage}: exit {rc}, see {logf}")


def stage_recognize(book):
    run_cmd(book, "recognize", [POST_PY, S / "make_pages_json.py", book.pdf, book.work / "pages.json"])
    n = book.pages()
    # generous hang guard: ~10x the measured 2.3 s/page, at least 1 h
    run_cmd(book, "recognize", ["bash", S / "run_mineru_full.sh", book.pdf, book.work, n],
            timeout=max(3600, n * 25))


def stage_layout(book):
    n = book.pages()
    run_cmd(book, "layout", ["bash", S / "run_layout_full.sh", book.pdf, book.work / "pages.json", book.work / "layout"],
            timeout=max(1800, n * 10))


def stage_post(book):
    run_cmd(book, "post", ["bash", S / "post_full.sh", book.pdf, book.dir, book.name, book.title])


def stage_qa(book):
    run_cmd(book, "qa", [POST_PY, S / "qa_book.py", book.pdf, book.dir, book.name])


STAGES = {"recognize": stage_recognize, "layout": stage_layout, "post": stage_post, "qa": stage_qa}


def record_failure(book, stage, err):
    with LOCK:
        failed = jload(BATCH / "failed.json", [])
        failed = [f for f in failed if not (f["book"] == book.name and f["stage"] == stage)]
        failed.append({"book": book.name, "pdf": str(book.pdf), "stage": stage, "error": str(err)[-2000:],
                       "log": str(book.logs / f"{stage}.log"), "time": now()})
        jdump(BATCH / "failed.json", failed)


def run_stage(book, stage):
    """Run one stage with one automatic retry. Returns True if done."""
    if book.done(stage):
        log(f"{book.name}: {stage} already done, skipped")
        return True
    st = jload(book.state / f"{stage}.json") or {}
    for attempt in (1, 2):
        t0 = time.time()
        started = now()
        jdump(book.state / f"{stage}.json", {"done": False, "running": True, "started": started,
                                             "attempts": st.get("attempts", 0) + attempt})
        log(f"{book.name}: {stage} start (attempt {attempt})")
        try:
            STAGES[stage](book)
        except Exception as e:                       # includes subprocess.TimeoutExpired
            el = round(time.time() - t0, 1)
            jdump(book.state / f"{stage}.json", {"done": False, "running": False, "started": started,
                                                 "failed_at": now(), "seconds": el, "error": str(e)[-1000:],
                                                 "attempts": st.get("attempts", 0) + attempt})
            log(f"{book.name}: {stage} FAILED attempt {attempt} after {el}s: {e}")
            if attempt == 2:
                record_failure(book, stage, e)
                return False
            continue
        el = round(time.time() - t0, 1)
        n = book.pages()
        jdump(book.state / f"{stage}.json", {"done": True, "started": started, "finished": now(), "seconds": el,
                                             "pages": n, "sec_per_page": round(el / n, 3) if n else None,
                                             "attempts": st.get("attempts", 0) + attempt})
        log(f"{book.name}: {stage} done in {el}s")
        return True


def post_and_qa(book):
    if run_stage(book, "post") and run_stage(book, "qa"):
        qa = jload(book.dir / "qa.json") or {}
        log(f"{book.name}: qa {qa.get('status')} {qa.get('reasons')}")
    finish_book(book)


FINISHED = []


def book_summary(book):
    qa = jload(book.dir / "qa.json") or {}
    secs = {s: (jload(book.state / f"{s}.json") or {}).get("seconds") for s in STAGES}
    failed = [f for f in jload(BATCH / "failed.json", []) if f["book"] == book.name]
    return {"book": book.name, "pages": book.pages(), "seconds": secs,
            "status": "failed" if failed else qa.get("status", "incomplete"),
            "reasons": qa.get("reasons") or [f"{f['stage']} 失败: {f['error'][-300:]}" for f in failed],
            "blank_pages_with_ink_in_scan": qa.get("blank_pages_with_ink_in_scan")}


def finish_book(book):
    with LOCK:
        FINISHED.append(book.name)
        prog = jload(BATCH / "progress.json", {"books": {}})
        prog["books"][book.name] = book_summary(book)
        prog["updated"] = now()
        jdump(BATCH / "progress.json", prog)
        with open(BATCH / "summary.csv", "w", encoding="utf-8-sig") as f:
            f.write("书名,页数,识别s,版面s,后处理s,qa s,合计s,状态,问题说明\n")
            for r in prog["books"].values():
                sec = r["seconds"]
                tot = sum(v or 0 for v in sec.values())
                why = "; ".join(r.get("reasons") or []).replace('"', "'")
                f.write(f'{r["book"]},{r["pages"]},{sec.get("recognize")},{sec.get("layout")},{sec.get("post")},'
                        f'{sec.get("qa")},{round(tot, 1)},{r["status"]},"{why}"\n')
        if len(FINISHED) % 10 == 0:
            write_report(prog, final=False)


def write_report(prog, final):
    rows = list(prog["books"].values())
    ok = [r for r in rows if r["status"] == "pass"]
    nr = [r for r in rows if r["status"] == "needs_review"]
    fl = [r for r in rows if r["status"] == "failed"]
    gpu_s = sum((r["seconds"].get("recognize") or 0) + (r["seconds"].get("layout") or 0) for r in rows)
    pages = sum(r["pages"] or 0 for r in rows if r["seconds"].get("recognize"))
    spp = gpu_s / pages if pages else None
    run_pages = prog.get("run_books_pages") or {}
    remaining = 0 if final else sum(v or 0 for k, v in run_pages.items() if k not in prog["books"] or
                                    prog["books"][k]["status"] == "incomplete")
    lines = [f"{now()}  已结束 {len(rows)} 本：pass {len(ok)}，needs_review {len(nr)}，失败 {len(fl)}",
             f"GPU（识别+版面）平均 {spp:.2f} 秒/页" if spp else "GPU 平均耗时：无数据"]
    if spp and remaining is not None:
        lines.append(f"剩余约 {remaining} 页，按 GPU 串行估计约 {remaining * spp / 3600:.1f} 小时")
    for r in nr + fl:
        lines.append(f"  [{r['status']}] {r['book']}: {'; '.join(r['reasons'] or [])}")
    with open(BATCH / ("final_report.txt" if final else "progress_report.txt"), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


COUNT_PY = r"""
import sys, json, pymupdf
out = {}
for p in sys.argv[2:]:
    try:
        out[p] = pymupdf.open(p).page_count
    except Exception as e:
        out[p] = None
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
"""


def page_counts(pdfs):
    f = BATCH / "page_counts.json"
    have = jload(f, {})
    missing = [str(p) for p in pdfs if str(p) not in have]
    if missing:
        subprocess.run([POST_PY, "-c", COUNT_PY, str(BATCH / "page_counts.tmp.json")] + missing, check=True)
        have.update(jload(BATCH / "page_counts.tmp.json", {}))
        (BATCH / "page_counts.tmp.json").unlink(missing_ok=True)
        jdump(f, have)
    return have


def free_gb():
    return shutil.disk_usage("/mnt/e").free / 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="book names (file stems) to process")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--root", default=str(SRC_ROOT))
    ap.add_argument("--retry-failed", action="store_true", help="also re-run books listed in failed.json")
    a = ap.parse_args()
    BATCH.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(pathlib.Path(a.root).rglob("*.pdf"), key=lambda p: str(p))
    if a.only:
        pdfs = [p for p in pdfs if p.stem in set(a.only)]
    if a.limit:
        pdfs = pdfs[:a.limit]
    (BATCH / "PAUSED.json").unlink(missing_ok=True)
    log(f"run_all start: {len(pdfs)} books, pid {os.getpid()}")
    pool = ThreadPoolExecutor(max_workers=2)
    futures = []
    books = [Book(p) for p in pdfs]
    counts = page_counts(pdfs)
    for k, book in enumerate(books):
        if all(book.done(s) for s in STAGES):
            log(f"{book.name}: all stages done, skipped")
            continue
        if any(f["book"] == book.name for f in jload(BATCH / "failed.json", [])):
            if not a.retry_failed:
                log(f"{book.name}: listed in failed.json, skipped (use --retry-failed to re-run)")
                continue
            with LOCK:
                jdump(BATCH / "failed.json", [f for f in jload(BATCH / "failed.json", []) if f["book"] != book.name])
            log(f"{book.name}: listed in failed.json, re-running (--retry-failed)")
        fg = free_gb()
        if fg < MIN_FREE_GB:
            log(f"PAUSE: E: free {fg:.1f} GB < {MIN_FREE_GB} GB before {book.name}; waiting for running post jobs")
            pool.shutdown(wait=True)
            jdump(BATCH / "PAUSED.json", {"time": now(), "free_gb": round(fg, 1), "next_book": book.name,
                                          "reason": f"E 盘剩余空间少于 {MIN_FREE_GB} GB"})
            sys.exit(3)
        with LOCK:
            prog = jload(BATCH / "progress.json", {"books": {}})
            prog["run_books_pages"] = {b.name: counts.get(str(b.pdf)) for b in books}
            prog["remaining_pages_at_start_of_current_book"] = sum(counts.get(str(b.pdf)) or 0 for b in books[k:])
            prog["total_books_this_run"] = len(books)
            jdump(BATCH / "progress.json", prog)
        if run_stage(book, "recognize") and run_stage(book, "layout"):
            futures.append(pool.submit(post_and_qa, book))
        else:
            finish_book(book)
    pool.shutdown(wait=True)
    for f in futures:
        if f.exception():
            log(f"post worker exception: {f.exception()}")
    prog = jload(BATCH / "progress.json", {"books": {}})
    write_report(prog, final=True)
    log("run_all finished")


if __name__ == "__main__":
    main()
