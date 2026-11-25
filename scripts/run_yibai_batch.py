#!/usr/bin/env python3
"""Crash-resilient batch OCR + EPUB 3 + dual-layer PDF runner for 壹佰本書認識花落人亡.

- Intermediate workdirs live under <repo>/ocr_yibai/<stem>/
- Final deliverables (<stem>.epub and <stem>_双层.pdf) are atomically delivered
  into the EXACT same directory as each source PDF (never touching the source PDF).
- Resumable per stage (recognize -> layout -> post -> qa -> deliver).
- Pauses safely if free disk space on /mnt/e or /mnt/h drops below 25 GB.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime

SCRIPTS = pathlib.Path(__file__).resolve().parent
OUT_ROOT = SCRIPTS.parent / "ocr_yibai"
MANIFEST = OUT_ROOT / "batch_manifest.json"
STATUS_JSON = OUT_ROOT / "batch_status.json"
PROGRESS_LOG = OUT_ROOT / "batch_progress.log"
POST_PY = "/opt/ocr/post/bin/python"
MIN_FREE_GB = 25.0


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    with PROGRESS_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def free_gb(path):
    st = shutil.disk_usage(path)
    return st.free / (1024 ** 3)


def atomic_copy(src: pathlib.Path, dst: pathlib.Path):
    tmp = dst.with_name(dst.name + ".copying.tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def run_cmd(cmd, log_file: pathlib.Path):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("wb") as lf:
        p = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        tail = ""
        try:
            tail = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        except Exception:
            pass
        raise RuntimeError(f"Command failed (rc={p.returncode}): {' '.join(map(str, cmd))}\n{tail}")


def save_status(status):
    tmp = STATUS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_JSON)


def process_book(entry, idx, total, status):
    src_pdf = pathlib.Path(entry["path"])
    stem = src_pdf.stem.strip()
    npages = int(entry["pages"])
    book_out = OUT_ROOT / stem
    work = book_out / "work"
    logs = book_out / "logs"
    state = book_out / "state"
    for d in (work, logs, state):
        d.mkdir(parents=True, exist_ok=True)

    dst_dir = src_pdf.parent
    dst_epub = dst_dir / f"{stem}.epub"
    dst_pdf = dst_dir / f"{stem}_双层.pdf"

    # Skip if already delivered cleanly
    if (state / "done_deliver.flag").exists() and dst_epub.exists() and dst_pdf.exists():
        log(f"[{idx}/{total}] SKIP (already delivered): {stem}")
        status["books"][stem] = {"state": "done", "pages": npages, "dst_dir": str(dst_dir)}
        save_status(status)
        return

    # Disk space guard on both E: and H:
    fe, fh = free_gb("/mnt/e"), free_gb("/mnt/h")
    if fe < MIN_FREE_GB or fh < MIN_FREE_GB:
        raise SystemExit(f"LOW DISK SPACE: E={fe:.1f}GB, H={fh:.1f}GB (< {MIN_FREE_GB}GB). Pausing safely.")

    t0 = time.time()
    log(f"[{idx}/{total}] START ({npages}p, {entry['mb']}MB): {stem} -> {dst_dir}")
    status["current"] = {"idx": idx, "total": total, "stem": stem, "pages": npages, "started_at": now_str()}
    save_status(status)

    # Stage 1: recognize (MinerU GPU)
    if not (state / "done_recognize.flag").exists():
        pages_json = work / "pages.json"
        if not pages_json.exists():
            run_cmd([POST_PY, str(SCRIPTS / "make_pages_json.py"), str(src_pdf), str(pages_json)], logs / "make_pages.log")
        run_cmd(["bash", str(SCRIPTS / "run_mineru_full.sh"), str(src_pdf), str(work), str(npages)], logs / "recognize.log")
        (state / "done_recognize.flag").write_text(now_str() + "\n", encoding="utf-8")
        log(f"[{idx}/{total}]   recognize done ({time.time()-t0:.0f}s): {stem}")

    # Stage 2: layout (PP-DocLayoutV3 GPU)
    if not (state / "done_layout.flag").exists():
        run_cmd(["bash", str(SCRIPTS / "run_layout_full.sh"), str(src_pdf), str(work / "pages.json"), str(work / "layout")], logs / "layout.log")
        (state / "done_layout.flag").write_text(now_str() + "\n", encoding="utf-8")
        log(f"[{idx}/{total}]   layout done ({time.time()-t0:.0f}s): {stem}")

    # Stage 3: post (adapt -> hybrid -> assemble EPUB 3 dual -> epubcheck -> build_pdf)
    if not (state / "done_post.flag").exists():
        run_cmd(["bash", str(SCRIPTS / "post_full.sh"), str(src_pdf), str(book_out), stem, stem], logs / "post.log")
        (state / "done_post.flag").write_text(now_str() + "\n", encoding="utf-8")
        log(f"[{idx}/{total}]   post done ({time.time()-t0:.0f}s): {stem}")

    # Stage 4: qa
    qa_verdict = "unknown"
    if not (state / "done_qa.flag").exists():
        try:
            run_cmd([POST_PY, str(SCRIPTS / "qa_book.py"), str(src_pdf), str(book_out), stem], logs / "qa.log")
        except Exception as e:
            log(f"[{idx}/{total}]   qa warning ({stem}): {e}")
        (state / "done_qa.flag").write_text(now_str() + "\n", encoding="utf-8")
    qa_path = book_out / "qa.json"
    if qa_path.exists():
        try:
            qa_verdict = json.loads(qa_path.read_text(encoding="utf-8")).get("verdict", "unknown")
        except Exception:
            pass

    # Stage 5: atomic deliver to original PDF's folder on H:
    src_epub = book_out / f"{stem}.epub"
    src_dual = book_out / f"{stem}_双层.pdf"
    if not src_epub.exists() or not src_dual.exists():
        raise RuntimeError(f"Missing output deliverables in {book_out}")
    atomic_copy(src_epub, dst_epub)
    atomic_copy(src_dual, dst_pdf)
    (state / "done_deliver.flag").write_text(now_str() + "\n", encoding="utf-8")

    elapsed = round(time.time() - t0, 1)
    log(f"[{idx}/{total}] DONE ({elapsed}s, qa={qa_verdict}): {dst_epub.name} + {dst_pdf.name} -> {dst_dir}")
    status["books"][stem] = {
        "state": "done",
        "pages": npages,
        "elapsed_s": elapsed,
        "qa": qa_verdict,
        "dst_dir": str(dst_dir),
    }
    status["completed"] = sum(1 for v in status["books"].values() if v.get("state") == "done")
    status["failed"] = sum(1 for v in status["books"].values() if v.get("state") == "failed")
    save_status(status)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-old-ocr", action="store_true", default=True)
    ap.add_argument("--only", default="", help="substring filter to run only matching book(s)")
    args = ap.parse_args()

    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    queue = list(m["must_ocr"])
    if args.include_old_ocr:
        queue.extend(x for x in m["old_ocr"] if int(x["pages"]) >= 10)
    if args.only:
        queue = [x for x in queue if args.only in x["rel"]]

    # Mark already-delivered 人文与理性的中国 if deliverables exist
    status = {
        "started_at": now_str(),
        "total": len(queue),
        "completed": 0,
        "failed": 0,
        "current": None,
        "books": {},
    }
    if STATUS_JSON.exists():
        try:
            old = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
            status["books"] = old.get("books", {})
        except Exception:
            pass

    log(f"=== BATCH START: {len(queue)} books ({sum(x['pages'] for x in queue)} pages) ===")
    for idx, entry in enumerate(queue, 1):
        src_pdf = pathlib.Path(entry["path"])
        stem = src_pdf.stem.strip()
        dst_epub = src_pdf.parent / f"{stem}.epub"
        dst_pdf = src_pdf.parent / f"{stem}_双层.pdf"
        if dst_epub.exists() and dst_pdf.exists() and dst_epub.stat().st_size > 10000 and dst_pdf.stat().st_size > 10000:
            st_dir = OUT_ROOT / stem / "state"
            st_dir.mkdir(parents=True, exist_ok=True)
            (st_dir / "done_deliver.flag").write_text("already_present\n", encoding="utf-8")
        try:
            process_book(entry, idx, len(queue), status)
        except SystemExit as e:
            log(f"PAUSED: {e}")
            status["current"] = {"paused": str(e), "at": now_str()}
            save_status(status)
            sys.exit(2)
        except Exception as e:
            log(f"[{idx}/{len(queue)}] FAILED ({stem}): {e}")
            status["books"][stem] = {"state": "failed", "error": str(e)[:500], "pages": entry["pages"]}
            status["failed"] = sum(1 for v in status["books"].values() if v.get("state") == "failed")
            save_status(status)
            continue

    status["current"] = None
    status["finished_at"] = now_str()
    save_status(status)
    log(f"=== BATCH FINISHED: completed={status['completed']}, failed={status['failed']} / {len(queue)} ===")


if __name__ == "__main__":
    main()
