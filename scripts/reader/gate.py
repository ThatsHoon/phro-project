"""PreToolUse Read gate: swap a supported document for its cached Markdown."""
import json
import os
import subprocess
import sys
from pathlib import Path

import reader
from reader import limits

INDEX_LINES = 2000
INDEX_CHARS = 10_000
INDEX_TITLE = 30  # characters of a heading in the context's index; index.md holds the whole title
CONTEXT_MAX = 600
DETAIL_MAX = 200  # characters of a failure detail in a deny reason (`_why`); the file controls some of it
READ_PY = Path(__file__).resolve().parents[1] / "read.py"

REASONS = {
    "unsupported": "this format is not supported yet",
    "encrypted": "the file is encrypted and needs a password",
    "drm": "the file is DRM-protected",
    "too_large": f"the file is over {limits.MAX_INPUT // 2**20} MiB",
    "timeout": f"conversion took over {limits.CONVERT_SECONDS} seconds",
}
LATE = "hook"  # timeout detail: the hook's own budget ran out, not the conversion's
LATE_TEXT = "reading the file used up the hook's time limit"
TOO_LARGE = {  # by the start of a too_large detail
    limits.BIG_PART: "a part expands too much to read safely",
    limits.BIG_TOTAL: f"the file expands past the {limits.ZIP_TOTAL // 2**30} GiB safety limit",
}
SKILL_OF = {**dict.fromkeys(("docx", "docm", "dotx", "dotm"), "docx"),
            **dict.fromkeys(("pptx", "pptm", "ppsx", "potx", "potm", "ppsm"), "pptx"),
            **dict.fromkeys(("xlsx", "xlsm", "xltx", "xltm"), "xlsx"),
            "pdf": "pdf"}
EDIT_HINT = " Read-only view (no XML, cell addresses or formulas); to edit the file, use the {skill} skill."
SAVE_AS = {**dict.fromkeys(("doc", "docx", "docm", "dotx", "dotm", "rtf", "odt", "wpd", "wp", "wp5", "wp6", "wp7",
                            "wpt", "pages"), "docx or pdf"),
           **dict.fromkeys(("xls", "xlsx", "xlsm", "xltx", "xltm", "xlsb", "ods", "numbers"), "xlsx or pdf"),
           **dict.fromkeys(("ppt", "pptx", "pptm", "ppsx", "potx", "potm", "ppsm", "odp", "key"), "pptx or pdf"),
           **dict.fromkeys(("hwp", "hwpx"), "hwpx or pdf")}
PROTECTED = " phro-project cannot open protected files; ask the user for an unprotected copy"
LARGE_PART = "the part they need saved on its own"

# pdf
PDF_HINT = ' Pages start at "## Page N" lines. Read-only view; to edit or create a pdf, use the pdf skill.'
OFFER = (' Built-in parser used; pypdfium2 (PyPI, 4 MB) reads tables, scans and forms better. Ask the user; only on'
         ' yes run: python -I -X utf8 "{read_py}" setup --venv "{dir}" (--decline instead if they never want it).')
REPAIR = (' The pdf engine failed its file check, so the built-in parser was used; to repair it, ask the user and only'
          ' on their yes run: python -I -X utf8 "{read_py}" setup --venv "{dir}".')
PAST_END = ' The pdf has {n} pages; pages="{a}-{b}" starts past its end.'
PASS_TEXT = "phro-project could not convert {name}: {why}. Claude's own Read is used."
DENY_KINDS = ("encrypted", "drm", "too_large")  # a pdf failing any other way goes to Claude's own Read
FAILED_SECONDS = 86_400  # a failed pdf passes through, and a timed-out conversion is denied, at once for a day
PRUNE_SECONDS = 86_400
SUGGEST_PAGES = 20  # Claude's own Read takes at most 20 pdf pages a request

# mpp and mpt: read only through MPXJ in the mpp venv; until it is ready every Read is denied with one of these
MPP_DENY = "{name}: Microsoft Project files are read only through MPXJ, which is not installed."
MPP_OFFER = (' MPXJ 16.10.0 with JPype 1.7.1 (PyPI, about 32 MB, LGPL-2.1 and Apache-2.0) reads it using the Java on'
             ' this machine. Ask the user whether to install it, and only on their yes run: python -I -X utf8'
             ' "{read_py}" setup --for mpp --venv "{dir}" (or the same with --decline if they never want it).')
MPP_NO_JAVA = " It also needs Java {floor} or later on PATH, which phro-project does not install."
MPP_DECLINED = ' (Declined; python -I -X utf8 "{read_py}" setup --for mpp installs it.)'
MPP_OFF = " The mpp engine is turned off (PHRO_READER_MPPENV=off)."
MPP_UNSUPPORTED ="Microsoft Project files are read only through MPXJ"  # formats.mpp.UNSUPPORTED: the child had no engine

# figures (WMF, EMF, PICT, WPG, TIFF) in every format but pdf and mpp: GDI+ draws them on Windows, the image venv
# (resvg_py, Pillow) elsewhere once it is set up; the offer comes when some were left undrawn for want of it
IMAGE_OFFER = (" {k} figures (WMF/EMF/PICT/WPG/TIFF) are not shown as images; their text is included. To show them,"
               ' ask the user, then run python -I -X utf8 "{read_py}" setup --for image --venv "{dir}" (--decline'
               " instead if they never want it).")
IMAGE_REPAIR = (" The image engine failed its file check, so {k} figures (WMF/EMF/PICT/WPG/TIFF) are not shown as"
                ' images; to repair it, ask the user and only on their yes run: python -I -X utf8 "{read_py}" setup'
                ' --for image --venv "{dir}".')


def off_exts():
    """The extensions PHRO_READER_OFF hands back to Read: comma-separated, stripped, case-folded, one dot dropped."""
    out = set()
    for item in os.environ.get("PHRO_READER_OFF", "").split(","):
        item = item.strip().casefold()
        item = item[1:] if item.startswith(".") else item
        if item:
            out.add(item)
    return frozenset(out)


def scope(event):
    """The document path when this event is a Read of an in-scope file, else None."""
    if not isinstance(event, dict) or event.get("tool_name") != "Read":
        return None
    ti = event.get("tool_input")
    fp = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(fp, str) or not fp:
        return None
    ext = reader.ext_of(fp)
    if ext not in reader.FORMATS or ext in off_exts():
        return None
    p = Path(fp)
    cwd = event.get("cwd")
    if not p.is_absolute() and isinstance(cwd, str) and cwd:
        p = Path(cwd) / p
    try:
        p = long_path(p)
        return p if p.is_file() else None
    except (OSError, ValueError):
        return None


def long_path(p):
    """On Windows, the \\\\?\\ form of an absolute path of 260 characters or more, which the file APIs accept even
    when long paths are not enabled; any other path as it is. The hash, the checks and the child all use it."""
    s = str(p)
    if sys.platform != "win32" or len(s) < 260 or s.startswith("\\\\?\\") or not p.is_absolute():
        return p
    s = os.path.normpath(s)  # the prefix turns off the API's own clean-up of "/" and ".."
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def _name(path):
    return " ".join(Path(path).name.split())


def looks_like_text(path):
    """True for a plain-text file (no NUL in the first 4 KiB, valid UTF-8, not RTF, no pdf header in 1 KiB)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if b"\x00" in head or b"%PDF-" in head[:1024]:
        return False
    for cut in range(4):
        try:
            text = head[:len(head) - cut].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    return not text.lstrip("﻿").startswith("{\\rtf")


def _is_cfb(path):
    """True when the file starts with the compound-file magic."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def _is_zip(path):
    """True when the file starts with the zip local-header signature (every Keynote file is a zip)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _next(path, kind):
    """The one sentence a deny of `kind` ends with: what can be done next. The convert command only for a timeout,
    the one failure that running it again can mend."""
    ext = reader.ext_of(path)
    skill = SKILL_OF.get(ext)
    if kind == "timeout":
        return (f' Retry by hand: python -I -X utf8 "{READ_PY}" convert "{path}"'
                + (f" or read it with the {skill} skill." if skill else ""))
    if kind in ("encrypted", "drm"):
        if skill == "pdf" and kind == "encrypted":
            return PROTECTED + ", or for the password and read it with the pdf skill."
        return PROTECTED + "."
    if kind == "too_large":
        skilled = f"read it with the {skill} skill instead, or " if skill else ""
        return f" That is phro-project's safety limit; {skilled}ask the user for a smaller file, such as {LARGE_PART}."
    ask = f" Read it with the {skill} skill instead, or ask" if skill else " Ask"
    again = "" if kind == "unsupported" else " again,"
    return f"{ask} the user for the file saved{again} as {SAVE_AS.get(ext, 'pdf')}."


def _why(kind, detail=""):
    """The reason a deny (or a pdf pass-through) gives for a failure kind and its detail, the detail cut to
    DETAIL_MAX characters (a zip member name in it is the file's, up to 64 KB)."""
    if len(detail) > DETAIL_MAX:
        detail = detail[:DETAIL_MAX - 1] + "…"
    if kind == "damaged" and detail.startswith("internal error"):
        return f"reading failed: {detail}"
    if kind == "damaged":
        return f"the file looks damaged: {detail}"
    if kind == "too_large":
        return TOO_LARGE.get(detail.split(":", 1)[0], REASONS["too_large"])
    if kind == "unsupported" and detail:
        return f"this file is not supported: {detail}"
    if kind == "timeout" and detail == LATE:
        return LATE_TEXT
    return REASONS.get(kind) or f"{kind} {detail}".strip()


def _deny(path, kind, detail=""):
    """A deny of failure `kind`: the reason, then what can be done next."""
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{_name(path)}: {_why(kind, detail)}." + _next(path, kind),
    }}


def _pass(path, why):
    """A pdf pass-through: Claude's own Read reads the file; the one-line reason rides as context (fact F2)."""
    why = " ".join(str(why).split())
    room = CONTEXT_MAX - 1 - len(PASS_TEXT.format(name=_name(path), why=""))
    if len(why) > room:
        why = why[:max(0, room - 1)] + "…"
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": PASS_TEXT.format(name=_name(path), why=why),
    }}


def _fit(items, room):
    """The leading whole items whose ", "-joined length fits in room."""
    out, used = [], 0
    for item in items:
        cost = len(item) + (2 if out else 0)
        if used + cost > room:
            break
        out.append(item)
        used += cost
    return out


# a heading is document text promoted into Claude's context: drop quotes and control characters
# (C0, DEL, C1, line and paragraph separators, bidi controls) before it is quoted there
_CLEAN = {c: " " for c in [*range(0x20), *range(0x7f, 0xa0), 0x2028, 0x2029, *range(0x202a, 0x202f),
                            *range(0x2066, 0x206a)]}
_CLEAN.update({ord(q): None for q in '"“”„‟″＂«»〝〞'})
_CLEAN.update({c: None for c in (*range(0x200b, 0x2010), 0x061c, 0x2060, 0xfeff)})  # zero-width and bidi marks


def _heading(text):
    return " ".join(str(text).translate(_CLEAN).split())


def _clause(prefix, entries, end, used):
    """prefix, the leading entries that fit beside `used` characters (", …" when some are cut), then end;
    empty when not one entry fits."""
    items = _fit(entries, CONTEXT_MAX - 1 - used - len(prefix) - len(", …") - len(end))
    if not items:
        return ""
    return prefix + ", ".join(items) + (", …" if len(items) < len(entries) else "") + end


def _ints(v):
    """The sorted distinct positive whole numbers in a meta list (anything else in it is ignored)."""
    if not isinstance(v, list):
        return []
    return sorted({x for x in v if isinstance(x, int) and not isinstance(x, bool) and x > 0})


def _runs(nums):
    """Sorted distinct numbers as (first, last) runs of consecutive values."""
    runs = []
    for x in nums:
        if runs and x == runs[-1][1] + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    return runs


def _span(a, b):
    return str(a) if a == b else f"{a}-{b}"


def page_index(meta):
    """meta's [[page, line], …] as (page, line) pairs of positive ints, in line order."""
    pairs = []
    for item in meta.get("pages") or []:
        if (isinstance(item, list) and len(item) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in item)):
            pairs.append((item[0], item[1]))
    return sorted(pairs, key=lambda pl: pl[1])


def _sample(prefix, entries, sep, room):
    """(prefix and every k-th entry, k) for the smallest k whose text fits in room, `sep` and "…" ending it when the
    last entry is not in the sample; ("", 0) when not even the first fits."""
    for k in range(1, len(entries) + 1):
        mark = "" if (len(entries) - 1) % k == 0 else sep + "…"
        text = prefix + sep.join(entries[::k]) + mark
        if len(text) <= room:
            return text, k
    return "", 0


def _write_index(target, head, lines):
    """index.md beside out.md: `head`, then one entry a line; True when it is there."""
    if target.is_file():
        return True
    tmp = target.with_name(f".tmp-index-{os.getpid()}")
    try:
        tmp.write_text(head + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return target.is_file()


def _index(meta, out_md, pdf, beside_assets=False):
    """(fit, floor) for the index the context gives a long output: for pdf ` Pages at lines: page:line, …`, for the
    rest ` Sections at lines: line title; …` (titles cut to INDEX_TITLE). fit(room) is the whole index when it fits
    in room; else every k-th entry for the smallest k that fits, and the whole index in index.md beside out.md,
    named in the prefix by its path, or as next to the assets folder when the context already gave that folder's
    path; else, index.md written, only the sentence naming it. floor is the room the whole index or that sentence
    takes, the least the context keeps for it; (None, 0) when there is no index."""
    if pdf:
        name, sep, head = " Pages at lines", ", ", "Pages of out.md, as page:line"
        entries = full = [f"{p}:{line}" for p, line in page_index(meta)]
    else:
        name, sep, head = " Sections at lines", "; ", "Sections of out.md, as line and title"
        heads = [(line, t) for line, text in meta.get("headings") or [] if (t := _heading(text))]
        entries = [f"{line} {t[:INDEX_TITLE].strip().replace(';', ',')}" for line, t in heads]
        full = [f"{line} {t}" for line, t in heads]
    if not entries:
        return None, 0
    target = out_md.parent / "index.md"
    where = "index.md next to that assets folder" if beside_assets else str(target)
    prefix = f"{name} (all in {where}): "
    whole = name + ": " + sep.join(entries)

    def fit(room):
        if len(whole) <= room:
            return whole
        if not _write_index(target, head, full):
            return _sample(name + ": ", entries, sep, room)[0]
        text = _sample(prefix, entries, sep, room)[0]
        return text or (pointer if len(pointer) <= room else "")
    pointer = f"{name}: {where}."
    return fit, min(len(whole), len(pointer))


def _window(meta, ti):
    """({"limit": n}, the sentence that says so) for a read without a limit whose Read window from its offset would
    pass READ_TOKENS or READ_BYTES by meta's line_bytes; ({}, "") when Read's own window fits or the offset is not a
    line number."""
    sizes = meta.get("line_bytes")
    off = ti.get("offset", 1)
    if ("limit" in ti or not isinstance(sizes, list) or not isinstance(off, int) or isinstance(off, bool)):
        return {}, ""
    s, m = max(1, off), len(sizes)
    stop = min(m, s - 1 + limits.READ_LINES)
    cap = min(limits.READ_TOKENS * limits.READ_TOKEN_BYTES, limits.READ_BYTES)
    used, e = 0, s - 1
    for b in sizes[s - 1:stop]:
        b = _count(b)
        if used + b > cap:
            break
        used, e = used + b, e + 1
    if e >= stop:
        return {}, ""
    e = max(e, s)
    return {"limit": e - s + 1}, f" Lines {s}-{e} of {m}; continue with offset={e + 1}."


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _past_cut(meta, upto=None, short=False):
    """The sentence naming the pages after the page index's last one, up to `upto` (the pdf's last page when None),
    and the pages= read that converts them, in fewer words when `short`; "" when there are none."""
    end = _count(meta.get("pages_total") if upto is None else upto)
    a = max((p for p, _ in page_index(meta)), default=0) + 1
    if a > end:
        return ""
    if a == end:
        return f' Page {a} is past the cut; Read the pdf with pages="{a}"' + ("." if short else " to read it.")
    if end - a < SUGGEST_PAGES:
        return (f' Pages {a}-{end} are past the cut; Read the pdf with pages="{a}-{end}"'
                + ("." if short else " to read them."))
    first = f' Pages {a}-{end} are past the cut; Read the pdf with pages="{a}-{a + SUGGEST_PAGES - 1}", then the next'
    return first + (f" {SUGGEST_PAGES}." if short else f" {SUGGEST_PAGES} pages and so on, to read them.")


def _pending(meta):
    """The pages of a partial pdf entry whose text the deadline left out: those whose pictures OCR did not read,
    and from the page where page OCR stopped to the page index's last."""
    if meta.get("partial") is not True:
        return []
    ocr = meta.get("ocr") if isinstance(meta.get("ocr"), dict) else {}
    pages = set(_ints(ocr.get("pictures_left_pages")))
    if at := _count(ocr.get("stopped_at")):
        pages.update(range(at, max([at] + [p for p, _ in page_index(meta)]) + 1))
    return sorted(pages)


def _reach(path, meta, out_md, short=False):
    """The sentence naming the read that gets what the deadline left out of a partial entry, in fewer words when
    `short`: for a pdf range entry the same pages= read again, for a whole pdf the pages= read of the first pages
    left (and how far they go), else the convert by hand that finishes the entry; "" when the entry is whole."""
    if meta.get("partial") is not True:
        return ""
    rest = " for the rest" if short else " for the text the time left out"
    if reader.ext_of(path) == "pdf":
        _, a, b = (out_md.parent.name.split("-") + ["", ""])[:3]
        if a[:1] == "p" and a[1:].isdigit() and b.isdigit():
            return f' Read the pdf with pages="{_span(int(a[1:]), int(b))}" again{rest}.'
        if pages := _pending(meta):
            a = pages[0]
            b = max(p for p in pages if p < a + SUGGEST_PAGES)
            if pages[-1] == b:
                more = "."
            elif short:
                more = f", then on to page {pages[-1]}."
            else:
                more = f", then pages {b + 1}-{pages[-1]}."
            return f' Read the pdf with pages="{_span(a, b)}"{rest}' + more
    run = f'python -I -X utf8 "{READ_PY}" convert "{path}"' + (" --engine pdfium" if meta.get("engine") == "pdfium"
                                                                else "")
    if short:
        return f" For the rest, run {run} and Read again."
    return f" To read the rest, run: {run}, then Read the file again."


def _time_cut(meta, short=False):
    """The sentence saying what the deadline left out of a partial entry, without the list when `short`; "" when
    the entry is whole."""
    if meta.get("partial") is not True:
        return ""
    ocr = meta.get("ocr") if isinstance(meta.get("ocr"), dict) else {}
    items = []
    if at := _count(ocr.get("stopped_at")):
        items.append(f"OCR stopped at page {at}" + ("" if short else " (later scanned pages are linked as images)"))
    if k := _count(ocr.get("pictures_left")):
        items.append(f"{k} picture{'' if k == 1 else 's'} not read by OCR"
                     + ("" if short else " (Read it to view)" if k == 1 else " (Read them to view)"))
    if limits.FIGURES_LATE in (meta.get("warnings") or []):
        items.append("some figures not drawn" + ("" if short else " (their text is included)"))
    if not items:
        return ""
    return " Cut by the time limit." if short else " Cut by the time limit: " + ", ".join(items) + "."


def _head(name, meta, assets):
    n = meta.get("lines", 0)
    m = meta.get("images", 0)
    k = meta.get("hidden", 0)
    head = f"{name} was converted to Markdown by phro-project ({n} line{'' if n == 1 else 's'}"
    if m:
        head += f", {m} image in {assets}; Read it to view" if m == 1 else f", {m} images in {assets}; Read them to view"
    head += ")."
    if k:
        head += f" {k} hidden place{'' if k == 1 else 's'} left out."
    return head


def _context(path, meta, out_md, *, past_end="", offer="", upto=None, window=""):
    """additionalContext for an allow, never past CONTEXT_MAX - 1 characters. Room goes first to the head (the file
    name cut short when it must), what the time cut, for pdf the pages past the cut (up to `upto`, the end of a
    range read) and the read that gets them, the `window` sentence, the garble warning, and the floor of a long
    output's index; then, for pdf, the past-the-end sentence, PDF_HINT, the offer (whole or not at all), the
    unrendered and figure pages, and for the rest the edit hint and the image offer (whole or not at all); then
    the unread list; the index takes what is left. They read head, hint, past-the-end, offer, pages, unread, index,
    then the time cut and the read that gets the rest, and the rest. The head names the assets folder by its
    absolute path: the Read result does not show out.md's."""
    name = _name(path)
    pdf = reader.ext_of(name) == "pdf"
    assets = f"{out_md.parent / 'assets'}{os.sep}"
    garble = " Warning: the text may be garbled." if "garbled" in (meta.get("warnings") or []) else ""

    def tail_of(short):
        return (_time_cut(meta, short) + _reach(path, meta, out_md, short) + (_past_cut(meta, upto, short) if pdf
                                                                               else "") + window + garble)
    tail = tail_of(False)
    n = meta.get("lines", 0)
    long_out = isinstance(n, int) and n > INDEX_LINES
    if not long_out:
        try:
            if out_md.stat().st_size > INDEX_CHARS:
                long_out = len(out_md.read_text(encoding="utf-8", errors="replace")) > INDEX_CHARS
        except OSError:
            pass
    fit, floor = _index(meta, out_md, pdf, bool(meta.get("images"))) if long_out else (None, 0)
    head = _head(name, meta, assets)
    over = len(head) + len(tail) + floor - (CONTEXT_MAX - 1)
    ext = reader.ext_of(name)
    stem = name[:len(name) - len(ext) - 1] if ext else name
    if over > 0 and len(stem) > 2:
        head = _head(stem[:max(1, len(stem) - over - 1)] + "…" + (f".{ext}" if ext else ""), meta, assets)
    if len(head) + len(tail) + floor > CONTEXT_MAX - 1:
        tail = tail_of(True)
    if len(head) + len(tail) + floor > CONTEXT_MAX - 1:
        fit, floor = None, 0
    if len(head) + len(tail) > CONTEXT_MAX - 1:
        head = head[:max(0, CONTEXT_MAX - 2 - len(tail))] + "…"
    parts = {}

    def used():
        return len(head) + len(tail) + floor + sum(len(v) for v in parts.values())

    def take(key, text):
        if text and used() + len(text) <= CONTEXT_MAX - 1:
            parts[key] = text
    if pdf:
        take("past_end", past_end)
        take("hint", PDF_HINT)
        runs = _runs(_ints(meta.get("unrendered")))

        def unrendered(extra):
            if not runs:
                return ""
            a, b = runs[0]
            end = f'; Read the pdf with pages="{a}-{min(b, a + SUGGEST_PAGES - 1)}" to see them.'
            return _clause(" Pages not shown as images: ", [_span(*r) for r in runs], end, used() + extra)
        if offer and used() + len(offer) <= CONTEXT_MAX - 1:
            # the offer comes again on a later Read; pages Claude cannot see here would be lost for good
            if unrendered(len(offer)) or not unrendered(0):
                parts["offer"] = offer
        take("unrendered", unrendered(0))
        runs = _runs(_ints(meta.get("figures")))
        if runs:
            take("figures", _clause(" Vector figures on pages ", [_span(*r) for r in runs], " are linked as images.",
                                    used()))
        order = ("hint", "past_end", "offer", "unrendered", "figures", "unread")
    else:
        skill = SKILL_OF.get(reader.ext_of(name))
        take("hint", EDIT_HINT.format(skill=skill) if skill else "")
        take("offer", offer)
        order = ("hint", "offer", "unread")
    unread = [" ".join(str(x).split()) for x in meta.get("unread") or []]
    if unread:
        take("unread", _clause(" Some parts could not be read: ", unread, ".", used()))
    body = head + "".join(parts.get(key, "") for key in order)
    index = fit(CONTEXT_MAX - 1 - len(body) - len(tail)) if fit else ""
    return body + index + tail


def _now():
    import time  # not at module level: the gate's imports stay light for every Read
    return time.monotonic()


def _windows():
    return sys.platform == "win32"


def image_backend(venv=None):
    """The figure backend id in the key of every format but pdf and mpp: "gdiplus" on Windows, where the image venv
    is never used, even when it exists; elsewhere pyenv.engine("image") ("resvg-…" when the venv is READY, else
    "none"). The convert child keys with this same function on Windows (read._image_id), so GDI+ is never started
    for a key; elsewhere it works the id out itself (raster.backend(), which also verifies the venv)."""
    if _windows():
        return "gdiplus"
    from reader import pyenv
    return pyenv.engine(kind="image", venv=venv if venv is not None else pyenv.venv_dir("image"))


def ocr_key(b) -> str:
    """An OCR backend (id, version) as the cache key carries it: joined with ":"; "" with no engine."""
    return ":".join(b) if b[0] else ""


def ocr_backend():
    """The OCR backend id and version in the key of every format but mpp: `ocr_probe.backend()` as `ocr_key`, from
    the engines' files alone, without importing reader.ocr. The convert child keys with this same probe and
    `ocr_key` (read._ocr_id)."""
    from reader import ocr_probe
    return ocr_key(ocr_probe.backend())


def _offer_mark(event):
    """The once-a-session marker of the setup offers (pdf's and the image venv's share it), or None with no session."""
    sid = event.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    import hashlib
    from reader import cache
    return cache.root() / "offers" / hashlib.sha1(sid.encode("utf-8", "replace")).hexdigest()


def _child(cmd, start, timeout):
    """Run a convert child: (entry, "", "") on success, else (None, kind, detail)."""
    # hashing a big file on slow storage counts against the hook's 60 seconds too
    left = limits.HOOK_SECONDS - (_now() - start)
    if left < limits.HOOK_CHILD_MIN:
        return None, "timeout", LATE
    budget = min(timeout, left)
    import time
    # the child's own children (soffice, wpd._via_soffice) must be gone before this gives up on it: it reads the
    # wall-clock deadline from PHRO_READER_DEADLINE
    env = dict(os.environ, PHRO_READER_DEADLINE=repr(time.time() + budget))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=budget, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout", LATE if budget < timeout else ""
    finally:
        _prune_daily()
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last.startswith("FAIL\t"):
        parts = last.split("\t", 2)
        return None, parts[1], parts[2] if len(parts) > 2 else ""
    out = Path(last) if last else None
    if r.returncode != 0 or out is None or out.name != "out.md" or not out.is_file():
        return None, "damaged", "conversion gave no output"
    return out.parent, "", ""


def _detached(cmd):
    """Starts cmd with no standard streams, in its own process group, and does not wait for it."""
    if _windows():
        kw = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kw = {"start_new_session": True}
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        pass


def _prune_daily():
    """After a miss: a detached `read.py prune` when the "pruned" marker in the cache root is PRUNE_SECONDS old,
    so the cache is pruned at most once a day off the Read's clock; a root without the marker only gets it."""
    import time
    from reader import cache
    mark = cache.root() / "pruned"
    try:
        due = time.time() - mark.stat().st_mtime >= PRUNE_SECONDS
    except OSError:
        _touch(mark)
        return
    if due:
        _touch(mark)
        _detached([sys.executable, "-I", "-X", "utf8", str(READ_PY), "prune"])


def _remember(mark, kind, detail):
    """Keeps a timeout of the conversion's own budget in `mark`, so the next Read is denied at once; a timeout the
    hook's remaining time caused (LATE) is not kept: the next Read has its full budget."""
    if kind == "timeout" and detail != LATE:
        _touch(mark, _why(kind, detail))


def _meta(entry):
    try:
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _allow(event, path, out_md, context, drop=(), **set_keys):
    updated = {k: v for k, v in event["tool_input"].items() if k not in drop}
    updated.update(set_keys, file_path=str(out_md))
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"phro-project converted {_name(path)} to Markdown",
        "updatedInput": updated,
        "additionalContext": context,
    }}


def decide(event, *, timeout=limits.CONVERT_SECONDS, command=None):
    start = _now()
    path = scope(event)
    if path is None:
        return None
    if looks_like_text(path):
        return None
    if reader.ext_of(path) == "key" and not _is_zip(path):
        return None  # a binary .key (a DER or PKCS key, say) is not a Keynote file
    if reader.ext_of(path) in ("vst", "mpt") and not _is_cfb(path):
        return None  # .vst is also a Targa image extension; a Visio or Project template is a compound file
    if reader.ext_of(path) not in reader.BUILT:
        return _deny(path, "unsupported")
    if path.stat().st_size > limits.MAX_INPUT:
        return _deny(path, "too_large", limits.BIG_INPUT)
    if reader.ext_of(path) == "pdf":
        return _pdf(event, path, start, timeout, command)
    if reader.ext_of(path) in ("mpp", "mpt"):
        return _mpp(event, path, start, timeout, command)
    return _generic(event, path, start, timeout, command)


def _generic(event, path, start, timeout, command):
    """decide() for every format but pdf and mpp: the figure backend id in the key. Off Windows, on a miss with the
    image venv's backend, the venv's file check and then the convert child under the venv's interpreter, so resvg_py
    and Pillow import in that one process; a venv that fails the check is not used (backend none, and the offer is
    the repair). The image setup offer when figures were left undrawn with backend none, once a session."""
    from reader import cache, pyenv
    repair = False
    venv = None if _windows() else pyenv.venv_dir("image")
    image, ocr = image_backend(venv), ocr_backend()
    k = cache.key(path, image=image, ocr=ocr)
    entry = cache.lookup(k)
    if entry is None and image.startswith("resvg-") and not pyenv.verify(venv, "image"):
        image, repair = "none", True  # the child works out "none" too: its own check fails the same way
        k = cache.key(path, image=image, ocr=ocr)
        entry = cache.lookup(k)
    if entry is None:
        mark = cache.root() / "failed" / k
        if _failed(mark) is not None:
            return _deny(path, "timeout")
        # the child hashes the bytes itself: a file saved after this hash must not be filed under the key
        if command:
            cmd = command
        else:
            py = pyenv.interpreter(venv) if image.startswith("resvg-") else sys.executable
            cmd = [str(py), "-I", "-X", "utf8", str(READ_PY), "convert", str(path)]
        try:
            entry, kind, detail = _child(cmd, start, timeout)
        except OSError as e:  # the venv's interpreter could not be started
            entry, kind, detail = None, "damaged", f"internal error {type(e).__name__}"
        if entry is None:
            _remember(mark, kind, detail)
            return _deny(path, kind, detail)
    out_md = entry / "out.md"
    if not out_md.is_file():
        return _deny(path, "damaged", "cache entry vanished")
    meta = _meta(entry)
    left = meta.get("figures_left")
    offer, mark = "", None
    if (not _windows() and image == "none" and isinstance(left, int) and not isinstance(left, bool) and left > 0
            and not pyenv.forced_native("image") and not pyenv.declined("image")):  # as pdf: never after a decline
        offer = (IMAGE_REPAIR if repair else IMAGE_OFFER).format(k=left, read_py=READ_PY, dir=venv)
        mark = _offer_mark(event)
        if mark is not None and mark.exists():
            offer = ""
    limit, window = _window(meta, event["tool_input"])
    context = _context(path, meta, out_md, offer=offer, window=window)
    if offer and mark is not None and offer in context:
        _touch(mark)
    return _allow(event, path, out_md, context, **limit)


def mpp_reason(path):
    """The mpp deny: MPP_DENY, then the engine-off note (PHRO_READER_MPPENV=off, where an install could never be
    used), the decline note, the Java note (no java on PATH, found the way setup and the venv's own check find it) or
    the install offer. Over CONTEXT_MAX the name is cut with "…", never a command: when READ_PY and the venv folder
    alone pass it, the name is "…" and the reason runs over."""
    from reader import pyenv
    if pyenv.forced_native("mpp"):
        tail = MPP_OFF
    elif pyenv.declined("mpp"):
        tail = MPP_DECLINED.format(read_py=READ_PY)
    elif pyenv.find_java() is None:
        tail = MPP_NO_JAVA.format(floor=pyenv.JAVA_FLOOR)
    else:
        tail = MPP_OFFER.format(read_py=READ_PY, dir=pyenv.venv_dir("mpp"))
    name = _name(path)
    room = CONTEXT_MAX - 1 - len(MPP_DENY.format(name="")) - len(tail)
    if len(name) > room:
        name = name[:max(0, room - 1)] + "…"
    return MPP_DENY.format(name=name) + tail


def _mpp_deny(path):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": mpp_reason(path),
    }}


def _mpp(event, path, start, timeout, command):
    """decide() for mpp and mpt: denied with the MPXJ offer unless the mpp venv is READY; then the engine id in the
    key and, on a miss, the venv's file check and the venv child (`--engine mpxj`), which hashes the file itself."""
    from reader import cache, pyenv
    venv = pyenv.venv_dir("mpp")
    engine = pyenv.engine(kind="mpp", venv=venv)
    if not engine:
        return _mpp_deny(path)
    k = cache.key(path, engine)
    entry = cache.lookup(k)
    if entry is None:
        mark = cache.root() / "failed" / k
        if _failed(mark) is not None:
            return _deny(path, "timeout")
        if not pyenv.verify(venv, "mpp"):  # a venv whose files changed after setup never runs
            return _mpp_deny(path)
        if command is not None:
            cmd = list(command)
        else:
            cmd = [str(pyenv.interpreter(venv)), "-I", "-X", "utf8", str(READ_PY), "convert"]
        cmd += [str(path), "--engine", "mpxj"]
        try:
            entry, kind, detail = _child(cmd, start, timeout)
        except OSError as e:  # the interpreter could not be started
            entry, kind, detail = None, "damaged", f"internal error {type(e).__name__}"
        if entry is None:
            if kind == "unsupported" and detail in ("", MPP_UNSUPPORTED):
                return _mpp_deny(path)
            _remember(mark, kind, detail)
            return _deny(path, kind, detail)
    out_md = entry / "out.md"
    if not out_md.is_file():
        return _deny(path, "damaged", "cache entry vanished")
    meta = _meta(entry)
    limit, window = _window(meta, event["tool_input"])
    return _allow(event, path, out_md, _context(path, meta, out_md, window=window), **limit)


def parse_pages(v):
    """Read's `pages` value as (A, B): `N` or `A-B` (up to 18 digits each), spaces allowed around the dash,
    1 <= A <= B; else None."""
    if not isinstance(v, str):
        return None
    nums = []
    for part in v.split("-"):
        part = part.strip()
        if not (part.isascii() and part.isdigit()) or len(part) > 18:
            return None
        nums.append(int(part))
    if len(nums) > 2:
        return None
    a, b = nums[0], nums[-1]
    return (a, b) if 1 <= a <= b else None


def offer_text(venv, repair=False):
    return (REPAIR if repair else OFFER).format(read_py=READ_PY, dir=venv)


def _failed(mark):
    """The reason kept in a failure marker younger than FAILED_SECONDS, else None."""
    import time
    try:
        if abs(time.time() - mark.stat().st_mtime) >= FAILED_SECONDS:
            return None
        why = " ".join(mark.read_text(encoding="utf-8", errors="replace")[:500].split())
    except OSError:
        return None
    return why or "an earlier conversion failed"


def _touch(mark, text=""):
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _all_unrendered(meta, a, b):
    """True when every page A..B (B within the pdf) is on meta's unrendered list."""
    unr = _ints(meta.get("unrendered"))
    return sum(1 for p in unr if a <= p <= b) == b - a + 1


class _Unverified(Exception):
    """The pdf venv failed its file check before a pdfium child was started."""


def _pdf(event, path, start, timeout, command):
    """decide() for pdf: the engine id in the key, Read's pages mapped onto the page index, failures passed
    through to Claude's own Read (remembered for a day) and the pypdfium2 setup offer once a session."""
    from reader import pyenv
    venv = pyenv.venv_dir()
    engine = pyenv.engine(kind="pdf", venv=venv)
    if engine != "native":
        try:
            return _pdf_read(event, path, start, timeout, command, venv, engine, False)
        except _Unverified:  # read natively under the native key, and offer the repair
            pass
    return _pdf_read(event, path, start, timeout, command, venv, "native", engine != "native")


def _pdf_read(event, path, start, timeout, command, venv, engine, repair):
    from reader import cache, pyenv
    k = cache.key(path, engine, ocr=ocr_backend())
    entry = cache.lookup(k)
    verified = []  # the venv's file check runs once, before the first pdfium child of this call

    def convert(key, pages=None):
        """(entry, None), or (None, the hook's output: a deny or a pass-through), or for `pages` that start past
        the pdf's end (None, None)."""
        got = cache.lookup(key)
        if got is not None and not (pages and cache.partial(got)):  # a partial range is converted again
            return got, None
        fail = _failed(cache.root() / "failed" / key)
        if fail is not None:
            return None, _pass(path, fail)
        if engine != "native" and not verified:
            verified.append(True)
            if not pyenv.verify(venv):
                raise _Unverified
        if command is not None:
            cmd = list(command)
        else:
            py = pyenv.interpreter(venv) if engine != "native" else sys.executable
            cmd = [str(py), "-I", "-X", "utf8", str(READ_PY), "convert"]
        cmd += [str(path), "--engine", "native" if engine == "native" else "pdfium"]
        if pages:
            cmd += ["--pages", f"{pages[0]}-{pages[1]}"]
        try:
            got, kind, detail = _child(cmd, start, timeout)
        except OSError as e:  # the interpreter could not be started
            got, kind, detail = None, "damaged", f"internal error {type(e).__name__}"
        if got is not None:
            return got, None
        if pages and kind == "damaged" and detail.startswith(f"pages {pages[0]}-{pages[1]} past the end"):
            return None, None
        if kind in DENY_KINDS:
            return None, _deny(path, kind, detail)
        why = _why(kind, detail)
        if not (kind == "timeout" and detail == LATE):  # the next Read has its full budget: let it try again
            _touch(cache.root() / "failed" / key, why)
        return None, _pass(path, why)

    rng = parse_pages(event["tool_input"].get("pages"))
    ranged = False
    if entry is None and rng is not None:
        entry, result = convert(f"{k}-p{rng[0]}-{rng[1]}", rng)
        if entry is None and result is not None:
            return result
        ranged = entry is not None
    if entry is None:
        entry, result = convert(k)
        if entry is None:
            return result
    if not (entry / "out.md").is_file():
        return _pass(path, _why("damaged", "cache entry vanished"))
    meta = _meta(entry)
    total = meta.get("pages_total")
    past_end, at, drop, upto = "", {}, ("pages",), None  # fact F4a: updatedInput replaces the input, so pages can go
    if rng is not None and isinstance(total, int) and not isinstance(total, bool):
        a, b = rng
        if a > total:
            past_end = PAST_END.format(n=total, a=rng[0], b=rng[1])
        else:
            b = min(b, total)
            if _all_unrendered(meta, a, b):
                return None
            drop = ("pages", "offset", "limit")  # the range decides what is read
            index = page_index(meta)
            if ranged:
                upto = b
            elif (not index or b > max(p for p, _ in index)  # past the cut, or left for the time: that range alone
                  or any(a <= p <= b for p in _pending(meta))):
                entry, result = convert(f"{k}-p{a}-{b}", (a, b))
                if entry is None:
                    return result
                if not (entry / "out.md").is_file():
                    return _pass(path, _why("damaged", "cache entry vanished"))
                meta, upto = _meta(entry), b
                if _all_unrendered(meta, a, b):
                    return None
            else:
                first = next(((p, line) for p, line in index if p >= a), None)
                if first is None or first[0] > b:  # none of the range was read: Claude's own Read reads it
                    return None
                at["offset"] = first[1]
                after = next((line for p, line in index if p > b), None)
                if after is not None:
                    at["limit"] = after - first[1]
    out_md = entry / "out.md"
    offer, mark = "", None
    if engine == "native" and not pyenv.forced_native() and not pyenv.declined():
        mark = _offer_mark(event)
        if mark is None or not mark.exists():
            offer = offer_text(venv, repair)
    limit, window = _window(meta, {**{k: v for k, v in event["tool_input"].items() if k not in drop}, **at})
    at.update(limit)
    context = _context(path, meta, out_md, past_end=past_end, offer=offer, upto=upto, window=window)
    if offer and mark is not None and offer in context:
        _touch(mark)
    return _allow(event, path, out_md, context, drop, **at)
