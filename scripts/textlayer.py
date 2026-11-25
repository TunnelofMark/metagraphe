"""Shared helpers: normalized document model + line segmentation + text-to-line allocation.

Normalized document (doc.json), produced by each tool adapter:
{
  "tool": "mineru-...", "book": "xigang", "source_pdf": "...",
  "pages": [
    {"page": <1-based page number in the ORIGINAL pdf>, "w": <display width pt>, "h": <display height pt>,
     "blocks": [
       {"type": "title|text|list|table|image|caption|header|footer|page_number|footnote|aside|formula|seal",
        "level": 1..n or null,                # heading level for titles
        "bbox": [x0, y0, x1, y1],             # display-space PDF points (page as shown, rotation applied)
        "text": "...",                        # plain text (tables: cells joined by space, rows by newline)
        "html": "<table>..</table>" | null,   # tables only
        "lines": [{"bbox": [...], "text": "..."}] | null,   # line geometry if the tool provides it
        "image": "images/xxx.png" | null}     # crop path relative to the doc.json folder
     ]}
  ]
}
"""
import re
import unicodedata
import numpy as np
import pymupdf

TEXTUAL = {"title", "text", "list", "caption", "header", "footer", "page_number", "footnote", "aside", "table", "toc"}
DPI = 150


_PAGE_CACHE = {}


def _page_gray(page, dpi):
    """Whole page rendered as displayed (rotation applied), cached. Cropping this ourselves avoids
    get_pixmap(clip=...) whose clip is not in display space on rotated pages."""
    key = (id(page.parent), page.number, dpi)
    if key not in _PAGE_CACHE:
        if len(_PAGE_CACHE) > 4:
            _PAGE_CACHE.clear()
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        _PAGE_CACHE[key] = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return _PAGE_CACHE[key]


def page_ink(page, clip, dpi=DPI):
    """Binary ink mask of a display-space clip, with table rules (long straight lines) removed."""
    full = _page_gray(page, dpi)
    s = dpi / 72.0
    x0, y0 = int(clip.x0 * s), int(clip.y0 * s)
    img = full[y0:max(y0 + 1, int(round(clip.y1 * s))), x0:max(x0 + 1, int(round(clip.x1 * s)))]
    ink = img < 128
    # remove table rules: straight ink runs longer than any glyph stroke (~14 pt), even if slightly skewed
    L = int(14 * dpi / 72)
    ink &= ~(_long_runs(ink, L, axis=0) | _long_runs(ink, L, axis=1))
    return ink, dpi / 72.0


def _long_runs(ink, L, axis):
    """Morphological opening with a 1xL line: pixels belonging to a straight run of >= L ink pixels.
    A 1-pixel tolerance across the run direction absorbs slight skew of scanned rules."""
    m = ink.copy()
    if axis == 0:
        m[:, 1:] |= ink[:, :-1]; m[:, :-1] |= ink[:, 1:]
    else:
        m[1:, :] |= ink[:-1, :]; m[:-1, :] |= ink[1:, :]
    if m.shape[axis] < L:
        return np.zeros_like(ink)
    c = np.cumsum(m, axis=axis, dtype=np.int32)
    c = np.concatenate([np.zeros_like(c.take([0], axis=axis)), c], axis=axis)
    n = m.shape[axis]
    win = c.take(range(L, n + 1), axis=axis) - c.take(range(0, n - L + 1), axis=axis)
    full = (win == L).astype(np.int32)          # full[j]: window [j, j+L) is all ink
    # dilate: pixel i is covered if any full window starts in [i-L+1, i]
    F = np.cumsum(full, axis=axis)
    F = np.concatenate([np.zeros_like(F.take([0], axis=axis)), F], axis=axis)   # F[k] = sum full[:k]
    i = np.arange(n)
    hi = np.minimum(i, n - L) + 1
    lo = np.maximum(i - L + 1, 0)
    cover = F.take(hi, axis=axis) - F.take(lo, axis=axis)
    return (cover > 0) & ink


def _runs(on, min_gap=1):
    runs, start, gap = [], None, 0
    for i, v in enumerate(on):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                runs.append([start, i - gap + 1]); start = None; gap = 0
    if start is not None:
        runs.append([start, len(on) - gap])
    return runs


def segment_lines(page, bbox, min_h_pt=3.0):
    """Split a horizontal-text region into text lines by horizontal ink projection.
    Returns display-space bboxes (pt), top to bottom."""
    clip = pymupdf.Rect(bbox) & page.rect
    if clip.is_empty or clip.height < min_h_pt:
        return []
    ink, s = page_ink(page, clip)
    rows = ink.sum(axis=1)
    on = rows > max(1, int(0.01 * ink.shape[1]))
    merged = []
    for r in _runs(on):
        if merged and r[0] - merged[-1][1] < 0.15 * max(1, (merged[-1][1] - merged[-1][0])):
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    lines = []
    for a, b in merged:
        if (b - a) / s < min_h_pt:
            continue
        cols = np.where(ink[a:b].any(axis=0))[0]
        lines.append([clip.x0 + cols[0] / s, clip.y0 + a / s, clip.x0 + (cols[-1] + 1) / s, clip.y0 + b / s])
    return lines


def segment_cells(page, line_box):
    """Split one text band into horizontal segments separated by gaps wider than ~1 character."""
    clip = pymupdf.Rect(line_box) & page.rect
    ink, s = page_ink(page, clip)
    if not ink.any():
        return []
    h = ink.shape[0]
    col = ink.sum(axis=0)
    on = col > 0
    # cell gaps in these tables are >= 2 characters; letter-spaced cells (荣　町) have ~1 char gaps
    segs = _runs(on, min_gap=max(2, int(1.8 * h)))
    # drop scan specks (narrow, little ink)
    segs = [(a, b) for a, b in segs if (b - a) >= 0.4 * h and col[a:b].sum() >= 0.15 * h * h]
    return [[clip.x0 + a / s, clip.y0, clip.x0 + b / s, clip.y1] for a, b in segs]


def _char_units(ch):
    """Approximate advance in em: CJK/full-width = 1, ASCII = 0.5."""
    return 0.5 if ord(ch) < 0x2E80 and ch not in "—…·" else 1.0


def allocate(text, line_boxes):
    """Distribute text over boxes proportionally to box widths (in em units)."""
    text = re.sub(r"\s+", "", text)
    if not line_boxes or not text:
        return []
    widths = [max(1e-3, b[2] - b[0]) for b in line_boxes]
    total_w = sum(widths)
    units = [_char_units(c) for c in text]
    total_u = sum(units)
    out, i, acc_w, u = [], 0, 0.0, 0.0
    for k, (box, w) in enumerate(zip(line_boxes, widths)):
        acc_w += w
        if k == len(line_boxes) - 1:
            j = len(text)
        else:
            target = total_u * acc_w / total_w
            j = i
            while j < len(text) and u + units[j] / 2 <= target:
                u += units[j]; j += 1
        if j > i:
            out.append({"bbox": box, "text": text[i:j]})
        i = j
    return out


def table_cells(html):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S)
    return [[re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c)) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in rows]


def _vertical_rules(page, clip, dpi=DPI):
    """x positions (pt) of vertical table rules inside clip: columns of long vertical ink runs."""
    full = _page_gray(page, dpi)
    s = dpi / 72.0
    x0, y0 = int(clip.x0 * s), int(clip.y0 * s)
    raw = full[y0:int(round(clip.y1 * s)), x0:int(round(clip.x1 * s))] < 128
    L = max(int(0.25 * raw.shape[0]), int(14 * s))
    v = _long_runs(raw, L, axis=0).any(axis=0)
    xs = [(a + b) / 2 for a, b in _runs(v, min_gap=2)]
    return [clip.x0 + x / s for x in xs]


def _cells_in_band(page, band, rules):
    """Cell boxes in one table row band: split at vertical rules, tighten each piece to its ink."""
    if not rules:
        return segment_cells(page, band)
    clip = pymupdf.Rect(band) & page.rect
    ink, s = page_ink(page, clip)
    cuts = [clip.x0] + [x for x in rules if clip.x0 < x < clip.x1] + [clip.x1]
    out = []
    for a, b in zip(cuts, cuts[1:]):
        i0, i1 = int((a - clip.x0) * s) + 2, int((b - clip.x0) * s) - 2
        if i1 <= i0:
            continue
        cols = np.where(ink[:, i0:i1].sum(axis=0) > 0)[0]
        if len(cols) and ink[:, i0:i1].sum() > 0.1 * ink.shape[0] ** 2:
            out.append([clip.x0 + (i0 + cols[0]) / s, clip.y0, clip.x0 + (i0 + cols[-1] + 1) / s, clip.y1])
    return out


def table_lines(page, blk):
    """Place table cell texts onto detected cells. Data rows are matched bottom-up (header rows with
    rowspans / multi-line cells rarely have the same count as visual bands); whatever header text
    is left is spread over the remaining top bands."""
    rows = [[c for c in r if c] for r in table_cells(blk.get("html"))]
    rows = [r for r in rows if r]
    bands = segment_lines(page, blk["bbox"])
    if not rows or not bands:
        return []
    rules = _vertical_rules(page, pymupdf.Rect(blk["bbox"]) & page.rect)
    out = []
    ri, bi = len(rows) - 1, len(bands) - 1
    while ri >= 0 and bi >= 0:
        cells, segs = rows[ri], _cells_in_band(page, bands[bi], rules)
        if len(segs) != len(cells) or len(cells) < 2:
            break                                   # reached the header (or a row we cannot match)
        # trailing space between cells: copy/paste gives "1962 121.83 ..." in every reader, not "1962121.83"
        out = [{"bbox": sg, "text": c + (" " if k < len(cells) - 1 else "")}
               for k, (sg, c) in enumerate(zip(segs, cells))] + out
        ri, bi = ri - 1, bi - 1
    if ri >= 0 and bi >= 0:
        head_text = "".join(c for r in rows[:ri + 1] for c in r)
        # header: spread over whole bands (cell slivers would give unreadably small glyph boxes)
        out = allocate(head_text, bands[:bi + 1]) + out
    return out


def reference_lines(src_page):
    """Visual lines of an existing (e.g. 超星) text layer: [{bbox, text}] in display space, fragments on the
    same baseline merged. Its characters are unreliable, but its line boxes and line breaks are good."""
    if src_page.rotation:
        return []
    frags = []
    for b in src_page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            if t and l["dir"][0] > 0.9:
                frags.append({"bbox": list(l["bbox"]), "text": t})
    frags.sort(key=lambda f: ((f["bbox"][1] + f["bbox"][3]) / 2, f["bbox"][0]))
    merged = []
    for f in frags:
        cy, h = (f["bbox"][1] + f["bbox"][3]) / 2, f["bbox"][3] - f["bbox"][1]
        for m in merged:
            mcy, mh = (m["bbox"][1] + m["bbox"][3]) / 2, m["bbox"][3] - m["bbox"][1]
            gap = max(m["bbox"][0], f["bbox"][0]) - min(m["bbox"][2], f["bbox"][2])
            if abs(cy - mcy) < 0.5 * max(h, mh) and gap < 1.2 * max(h, mh):   # same line, same column
                if f["bbox"][0] >= m["bbox"][0]:
                    m["text"] += f["text"]
                else:
                    m["text"] = f["text"] + m["text"]
                m["bbox"] = [min(m["bbox"][0], f["bbox"][0]), min(m["bbox"][1], f["bbox"][1]),
                             max(m["bbox"][2], f["bbox"][2]), max(m["bbox"][3], f["bbox"][3])]
                break
        else:
            merged.append(dict(f))
    return merged


def _nfkc1(c):
    n = unicodedata.normalize("NFKC", c)
    return n if len(n) == 1 else c


def snap_to_reference(text, ref, min_sim=0.5):
    """Split `text` at the line breaks of reference lines (bad OCR, good geometry) by aligning the
    two strings with an edit-distance alignment. Returns [{bbox, text}] or None if they don't match."""
    from rapidfuzz.distance import Levenshtein
    P = re.sub(r"\s+", "", text)
    parts = [re.sub(r"\s+", "", r["text"]) for r in ref]
    R = "".join(parts)
    if not P or not R:
        return None
    Pn, Rn = "".join(_nfkc1(c) for c in P), "".join(_nfkc1(c) for c in R)
    if 1 - Levenshtein.distance(Rn, Pn) / max(len(Pn), len(Rn)) < min_sim:
        return None
    m = [None] * (len(R) + 1)
    for tag, i1, i2, j1, j2 in Levenshtein.opcodes(Rn, Pn):
        for k in range(i1, i2):
            m[k] = j1 + min(k - i1, max(0, j2 - j1 - 1)) if tag in ("equal", "replace") else j1
    m[len(R)] = len(P)
    for k in range(len(R) - 1, -1, -1):          # fill gaps, keep monotonic
        if m[k] is None:
            m[k] = m[k + 1]
    out, s = [], 0
    for idx, (r, part) in enumerate(zip(ref, parts)):
        s_next = s + len(part)
        a = 0 if idx == 0 else m[s]
        b = len(P) if idx == len(ref) - 1 else m[s_next]
        if b > a:
            out.append({"bbox": r["bbox"], "text": P[a:b]})
        s = s_next
    return out


def _refs_in(ref_lines, bbox, pad=2.0):
    x0, y0, x1, y1 = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    return [r for r in ref_lines
            if x0 <= (r["bbox"][0] + r["bbox"][2]) / 2 <= x1 and y0 <= (r["bbox"][1] + r["bbox"][3]) / 2 <= y1]


def block_lines(page, blk, ref_lines=None):
    """Line geometry for a block: tool-provided lines if any; else snap the block text onto the line breaks
    of a reference text layer (old 超星 layer) when it matches; else segmentation + proportional allocation."""
    if blk.get("lines"):
        return [l for l in blk["lines"] if l.get("text", "").strip()]
    if ref_lines and blk["type"] in ("text", "list", "title", "caption", "footnote", "toc"):
        refs = [r for bb in (blk.get("bboxes") or [blk["bbox"]]) for r in _refs_in(ref_lines, bb)]
        snapped = snap_to_reference(blk.get("text") or "", refs) if refs else None
        if snapped:
            return snapped
    text = blk.get("text") or ""
    if not text.strip() and blk["type"] != "table":
        return []
    x0, y0, x1, y1 = blk["bbox"]
    flat = re.sub(r"\s+", "", text)
    if (x1 - x0) < 30 and (y1 - y0) > 2 * (x1 - x0) and len(flat) > 1:
        # narrow tall block = vertical text (map titles, 题词, side tabs): stack glyphs downwards
        return [{"bbox": blk["bbox"], "text": flat, "vertical": True}]
    if blk["type"] == "table":
        return table_lines(page, blk) or [{"bbox": blk["bbox"], "text": flat}]
    boxes = [ln for bb in blk.get("bboxes") or [blk["bbox"]] for ln in segment_lines(page, bb)]
    return allocate(text, boxes) or [{"bbox": blk["bbox"], "text": flat}]


LATEX = [
    (r"\leqslant", "≤"), (r"\geqslant", "≥"), (r"\leq", "≤"), (r"\geq", "≥"),
    (r"\le ", "≤ "), (r"\ge ", "≥ "),
    (r"\approx", "≈"), (r"\neq", "≠"), (r"\ne ", "≠ "), (r"\infty", "∞"),
    (r"\rightarrow", "→"), (r"\leftarrow", "←"), (r"\leftrightarrow", "↔"), (r"\to ", "→ "),
    (r"^{\circ}", "°"), (r"\degree", "°"), (r"\circ", "°"),
    (r"\times", "×"), (r"\cdot", "·"), (r"\sim", "~"), (r"\pm", "±"), (r"\mp", "∓"), (r"\div", "÷"),
    (r"\sqrt", "√"), (r"\angle", "∠"), (r"\perp", "⊥"), (r"\parallel", "∥"),
    (r"\%", "%"), (r"\prime", "'"), (r"\qquad", "  "), (r"\quad", " "),
    (r"\,", ""), (r"\;", ""), (r"\ ", ""),
    # Literal 1-to-1 Unicode rendering for common Greek / phonetic / editorial LaTeX macros
    (r"\varepsilon", "ε"), (r"\epsilon", "ε"), (r"\varsigma", "ς"), (r"\varphi", "φ"),
    (r"\alpha", "α"), (r"\beta", "β"), (r"\gamma", "γ"), (r"\delta", "δ"), (r"\zeta", "ζ"),
    (r"\eta", "η"), (r"\theta", "θ"), (r"\kappa", "κ"), (r"\lambda", "λ"), (r"\mu", "μ"),
    (r"\nu", "ν"), (r"\xi", "ξ"), (r"\pi", "π"), (r"\rho", "ρ"), (r"\sigma", "σ"),
    (r"\tau", "τ"), (r"\phi", "φ"), (r"\chi", "χ"), (r"\psi", "ψ"), (r"\omega", "ω"),
    (r"\Gamma", "Γ"), (r"\Delta", "Δ"), (r"\Theta", "Θ"), (r"\Lambda", "Λ"),
    (r"\Sigma", "Σ"), (r"\Phi", "Φ"), (r"\Psi", "Ψ"), (r"\Omega", "Ω"),
    (r"\partial", "∂"), (r"\int", "∫"),
    (r"\downarrow", "↓"), (r"\uparrow", "↑"), (r"\neg", "¬"), (r"\vee", "∨"), (r"\S", "§"),
    (r"\varnothing", "∅"), (r"\emptyset", "∅"), (r"\ell", "ℓ"),
]

_LATEX_ENV_NAMES = r"(?:array|aligned|align\*?|cases|matrix|pmatrix|bmatrix|vmatrix|gathered)"
_LATEX_RESIDUAL_RX = re.compile(
    r"\\(?:left|right|begin\{?" + _LATEX_ENV_NAMES + r"|end\{?" + _LATEX_ENV_NAMES
    + r"|text(?:bf)?|mathrm|mathbf|mathfrak|mathit|frac|Omega|omega|Delta|Sigma|Phi|Psi|Theta|Lambda"
    + r"|alpha|beta|gamma|delta|epsilon|varepsilon|lambda|mu|pi|rho|sigma|tau|phi|varphi|chi|psi"
    + r"|leqslant|geqslant|leq|geq|approx|neq|times|cdot|sim|pm|div|circ|degree|infty|sqrt)"
)


def _strip_latex_envs_and_delims(s):
    """Strip LaTeX environment wrappers (array/aligned/cases) and \\left/\\right delimiters."""
    has_env = bool(re.search(r"\\(?:begin|end)\{?" + _LATEX_ENV_NAMES, s))
    # Braced environments: \begin{array}{l} ... \end{array}
    s = re.sub(r"\\begin\{" + _LATEX_ENV_NAMES + r"\}(?:\{[^{}]*\})?", "", s)
    s = re.sub(r"\\end\{" + _LATEX_ENV_NAMES + r"\}", "", s)
    # Already brace-stripped residual environments: \beginarrayl, \beginaligned, \endarray, \endaligned
    s = re.sub(r"\\beginarray[lcr|]*(?=\s|[^a-z]|$)", "", s)
    s = re.sub(r"\\begin(?:aligned|align|cases|matrix|pmatrix|bmatrix|vmatrix|gathered)\b", "", s)
    s = re.sub(r"\\end(?:array|aligned|align|cases|matrix|pmatrix|bmatrix|vmatrix|gathered)\b", "", s)
    # \left / \right wrappers: drop invisible '.' and grouping braces '\{'/'\}', keep visible brackets
    s = re.sub(r"\\(?:left|right)\s*\\[\{\}]", "", s)
    s = re.sub(r"\\(?:left|right)\s*\\([|])", r"\1", s)
    s = re.sub(r"\\(?:left|right)\s*\.", "", s)
    s = re.sub(r"\\(?:left|right)\s*([(\[\]|)])", r"\1", s)
    s = re.sub(r"\\(?:left|right)(?![A-Za-z])", "", s)
    # Remove stray backslash directly before stripped brace/env (e.g. \left\\beginarrayl -> \)
    if has_env:
        s = re.sub(r"\\\\\s*", "；", s)
        s = re.sub(r"\\&", "；", s)
        s = re.sub(r"(?:^|\s)&(?:\s|$)", " ", s)
        s = re.sub(r"\\(?=[；\s]|$)", "", s)
        s = re.sub(r"^[；\s]+|[；\s]+$", "", s)
    return s


def _delatex(s):
    """Render unambiguous LaTeX markup to plain Unicode without altering content characters."""
    if not s:
        return s
    s = _strip_latex_envs_and_delims(s)
    # Fractions: \frac{a}{b} -> a/b
    for _ in range(2):
        s2 = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", s)
        if s2 == s:
            break
        s = s2
    # Unwrap common text wrappers first (handles nested/repeated occurrences cleanly)
    for _ in range(3):
        s2 = re.sub(r"\\(?:mathrm|text|textbf|mathbf|mathfrak|mathit)\{([^{}]*)\}", r"\1", s)
        if s2 == s:
            break
        s = s2
    # Also strip bare unbraced text/font commands (e.g. \mathrm m^2, \mathbf 1, \text 零售价格)
    s = re.sub(r"\\(?:mathrm|text|textbf|mathbf|mathfrak|mathit)\b\s*", "", s)
    # Combining diacritics common in dialect IPA transcriptions (e.g. \tilde{\alpha} -> α̃)
    s = re.sub(r"\\tilde\{([^{}]+)\}", lambda m: _delatex(m.group(1)) + "\u0303", s)
    s = re.sub(r"\\bar\{([^{}]+)\}", lambda m: _delatex(m.group(1)) + "\u0304", s)
    # Footnote circled-number superscripts: ^{①} -> ①
    s = re.sub(r"\^\{\s*([\u2460-\u2473\u2474-\u2487])\s*\}", r"\1", s)
    s = re.sub(r"\^([\u2460-\u2473\u2474-\u2487])", r"\1", s)
    for a, b in LATEX:
        s = s.replace(a, b)
    # Remove trailing \le / \ge right before a digit or symbol
    s = re.sub(r"\\le(?![A-Za-z])", "≤", s)
    s = re.sub(r"\\ge(?![A-Za-z])", "≥", s)
    # Clean ^{'} or ^{\prime} already converted to ^{'}
    s = s.replace("^{'}", "'").replace("^'", "'")
    s = re.sub(r" {2,}", " ", s)
    return s.replace("{", "").replace("}", "").strip()


def clean_residual_latex(s):
    """Idempotently clean residual LaTeX commands in already-adapted doc.json / table text
    without altering normal prose or Markdown punctuation."""
    if not s or "\\" not in s or not _LATEX_RESIDUAL_RX.search(s):
        return s
    s = _strip_latex_envs_and_delims(s)
    for _ in range(2):
        s2 = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", s)
        if s2 == s:
            break
        s = s2
    for _ in range(3):
        s2 = re.sub(r"\\(?:mathrm|text|textbf|mathbf|mathfrak|mathit)\{([^{}]*)\}", r"\1", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\\(?:mathrm|text|textbf|mathbf|mathfrak|mathit)\b\s*", "", s)
    for a, b in LATEX:
        if a in s:
            s = s.replace(a, b)
    s = re.sub(r"\\le(?![A-Za-z])", "≤", s)
    s = re.sub(r"\\ge(?![A-Za-z])", "≥", s)
    # Clean leftover \left / \right backslashes around IPA or array segments
    s = re.sub(r"\\(?=\s|$)", "", s)
    return s


def clean_equation(s):
    """Clean raw LaTeX from MinerU `equation_inline` spans / `equation` blocks (which lack $ delimiters)."""
    if not s:
        return s
    s = s.strip()
    if (s.startswith("$$") and s.endswith("$$")) or (s.startswith("$") and s.endswith("$")):
        s = s.strip("$").strip()
    return _delatex(s)


def clean_html_equations(raw_html):
    """Replace <eq>raw_latex</eq> inside HTML tables with de-LaTeXed plain text (preserving HTML entities)."""
    if not raw_html:
        return raw_html
    raw_html = re.sub(
        r"<eq\b[^>]*>(.*?)</eq>",
        lambda m: clean_equation(m.group(1)),
        raw_html,
        flags=re.I | re.S,
    )
    raw_html = re.sub(r"</?eq\b[^>]*>", "", raw_html, flags=re.I)
    return clean_residual_latex(raw_html)


def clean_text(s):
    r"""Undo VLM formatting artifacts in plain text: inline LaTeX ($2\times1000$, 17.2\%) and the
    line breaks some tools insert around inline formulas. Content characters are never changed."""
    if not s:
        return s
    s = re.sub(r"\$\$?\s*([^$\n]{1,300}?)\s*\$\$?", lambda m: _delatex(m.group(1)), s)
    s = re.sub(r"\\\(\s*([^\n]{1,300}?)\s*\\\)", lambda m: _delatex(m.group(1)), s)
    s = clean_residual_latex(s)
    s = s.replace(r"\%", "%").replace(r"\times", "×")
    # MinerU wraps inline formulas as "...增长 \n17.2%\n ，..." -> rejoin
    s = re.sub(r"\s*\n([0-9.]+%?)\n\s*", r"\1", s)
    return s
