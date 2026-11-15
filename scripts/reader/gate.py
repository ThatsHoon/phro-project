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
CONTEXT_MAX = 600
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

# pdf
PDF_HINT = ' Pages start at "## Page N" lines. Read-only view; to edit or create a pdf, use the pdf skill.'
OFFER = (' Built-in parser used; pypdfium2 (PyPI, 4 MB) reads tables, scans and forms better. Ask the user; only on'
         ' yes run: python -I -X utf8 "{read_py}" setup --venv "{dir}" (--decline instead if they never want it).')
REPAIR = (' The pdf engine failed its file check, so the built-in parser was used; to repair it, ask the user and only'
          ' on their yes run: python -I -X utf8 "{read_py}" setup --venv "{dir}".')
PAST_END = ' The pdf has {n} pages; pages="{a}-{b}" starts past its end.'
PASS_TEXT = "phro-project could not convert {name} ({why}); Claude's own Read is used."
DENY_KINDS = ("encrypted", "drm", "too_large")  # a pdf failing any other way goes to Claude's own Read
FAILED_SECONDS = 86_400  # a failed pdf passes through at once for a day
SUGGEST_PAGES = 20  # Claude's own Read takes at most 20 pdf pages a request


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


def deny_text(path, why, retry=True):
    """A deny; with retry, the convert command too (only where running it by hand can help)."""
    reason = f"{_name(path)}: {why}."
    skill = SKILL_OF.get(reader.ext_of(path))
    if retry:
        reason += f' Retry by hand: python -I -X utf8 "{READ_PY}" convert "{path}"'
        if skill:
            reason += f" or read it with the {skill} skill."
    elif skill:
        reason += f" Read it with the {skill} skill instead."
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _why(kind, detail=""):
    """The reason a deny (or a pdf pass-through) gives for a failure kind and its detail."""
    if kind == "damaged" and detail.startswith("internal error"):
        return f"reading failed ({detail})"
    if kind == "damaged":
        return f"the file looks damaged ({detail})"
    if kind == "too_large":
        return TOO_LARGE.get(detail.split(":", 1)[0], REASONS["too_large"])
    if kind == "unsupported" and detail:
        return f"this file is not supported ({detail})"
    if kind == "timeout" and detail == LATE:
        return LATE_TEXT
    return REASONS.get(kind) or f"{kind} {detail}".strip()


def _deny(path, kind, detail=""):
    return deny_text(path, _why(kind, detail), retry=kind in ("damaged", "timeout"))


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
    return " ".join(str(text).translate(_CLEAN).split())[:60].strip()


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


def _page_lines(meta, used):
    """` Pages at lines: p:line, …` with every k-th page, for the smallest k that fits."""
    prefix = " Pages at lines: "
    entries = [f"{p}:{line}" for p, line in page_index(meta)]
    for k in range(1, len(entries) + 1):
        mark = "" if (len(entries) - 1) % k == 0 else ", …"  # ", …" when the last page is not in the sample
        text = prefix + ", ".join(entries[::k]) + mark
        if used + len(text) <= CONTEXT_MAX - 1:
            return text
    return ""


def _head(name, meta):
    n = meta.get("lines", 0)
    m = meta.get("images", 0)
    k = meta.get("hidden", 0)
    head = f"{name} was converted to Markdown by phro-project ({n} line{'' if n == 1 else 's'}"
    if m:
        head += f", {m} image in assets/; Read it to view" if m == 1 else f", {m} images in assets/; Read them to view"
    head += ")."
    if k:
        head += f" {k} hidden places left out."
    return head


def _context(name, meta, out_md, *, past_end="", offer=""):
    """additionalContext for an allow, fitted under CONTEXT_MAX: for pdf, the head, PDF_HINT, the past-the-end
    sentence, the offer (whole or not at all), the unrendered and figure pages, the unread list and the page index;
    for the rest, the head, the edit hint, the unread list and the heading index. The garble warning goes last."""
    pdf = reader.ext_of(name) == "pdf"
    head = _head(name, meta)
    tail = ""
    if "garbled" in (meta.get("warnings") or []):
        tail = " Warning: the text may be garbled."
    if pdf:
        head += PDF_HINT + past_end
        runs = _runs(_ints(meta.get("unrendered")))

        def unrendered(used):
            if not runs:
                return ""
            a, b = runs[0]
            end = f'; Read the pdf with pages="{a}-{min(b, a + SUGGEST_PAGES - 1)}" to see them.'
            return _clause(" Pages not shown as images: ", [_span(*r) for r in runs], end, used)
        if offer and len(head) + len(offer) + len(tail) <= CONTEXT_MAX - 1:
            # the offer comes again on a later Read; pages Claude cannot see here would be lost for good
            if unrendered(len(head) + len(offer) + len(tail)) or not unrendered(len(head) + len(tail)):
                head += offer
        head += unrendered(len(head) + len(tail))
        runs = _runs(_ints(meta.get("figures")))
        if runs:
            head += _clause(" Vector figures on pages ", [_span(*r) for r in runs], " are linked as images.",
                            len(head) + len(tail))
    else:
        skill = SKILL_OF.get(reader.ext_of(name))
        head += EDIT_HINT.format(skill=skill) if skill else ""
    unread = [" ".join(str(x).split()) for x in meta.get("unread") or []]
    if unread:
        head += _clause(" Some parts could not be read: ", unread, ".", len(head) + len(tail))
    n = meta.get("lines", 0)
    long_out = isinstance(n, int) and n > INDEX_LINES
    if not long_out:
        try:
            if out_md.stat().st_size > INDEX_CHARS:
                long_out = len(out_md.read_text(encoding="utf-8", errors="replace")) > INDEX_CHARS
        except OSError:
            pass
    index = ""
    heads = meta.get("headings") or []
    if long_out and pdf:
        index = _page_lines(meta, len(head) + len(tail))
    elif long_out and heads:
        entries = [f'{line} "{t}"' for line, text in heads if (t := _heading(text))]
        index = _clause(" Sections at lines: ", entries, "", len(head) + len(tail))
    return head + index + tail


def _now():
    import time  # not at module level: the gate's imports stay light for every Read
    return time.monotonic()


def _child(cmd, start, timeout):
    """Run a convert child: (entry, "", "") on success, else (None, kind, detail)."""
    # hashing a big file on slow storage counts against the hook's 60 seconds too
    left = limits.HOOK_SECONDS - (_now() - start)
    if left < limits.HOOK_CHILD_MIN:
        return None, "timeout", LATE
    budget = min(timeout, left)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=budget)
    except subprocess.TimeoutExpired:
        return None, "timeout", LATE if budget < timeout else ""
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last.startswith("FAIL\t"):
        parts = last.split("\t", 2)
        return None, parts[1], parts[2] if len(parts) > 2 else ""
    out = Path(last) if last else None
    if r.returncode != 0 or out is None or out.name != "out.md" or not out.is_file():
        return None, "damaged", "conversion gave no output"
    return out.parent, "", ""


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
    if reader.ext_of(path) not in reader.BUILT:
        return _deny(path, "unsupported")
    if path.stat().st_size > limits.MAX_INPUT:
        return _deny(path, "too_large", limits.BIG_INPUT)
    if reader.ext_of(path) == "pdf":
        return _pdf(event, path, start, timeout, command)
    from reader import cache
    k = cache.key(path)
    entry = cache.lookup(k)
    if entry is None:
        # the child hashes the bytes itself: a file saved after this hash must not be filed under k
        cmd = command or [sys.executable, "-I", "-X", "utf8", str(READ_PY), "convert", str(path)]
        entry, kind, detail = _child(cmd, start, timeout)
        if entry is None:
            return _deny(path, kind, detail)
    out_md = entry / "out.md"
    if not out_md.is_file():
        return _deny(path, "damaged", "cache entry vanished")
    return _allow(event, path, out_md, _context(_name(path), _meta(entry), out_md))


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
    from reader import pdfenv
    venv = pdfenv.venv_dir()
    engine = pdfenv.engine(venv)
    if engine != "native":
        try:
            return _pdf_read(event, path, start, timeout, command, venv, engine, False)
        except _Unverified:  # read natively under the native key, and offer the repair
            pass
    return _pdf_read(event, path, start, timeout, command, venv, "native", engine != "native")


def _pdf_read(event, path, start, timeout, command, venv, engine, repair):
    import hashlib
    from reader import cache, pdfenv
    k = cache.key(path, engine)
    entry = cache.lookup(k)
    verified = []  # the venv's file check runs once, before the first pdfium child of this call

    def convert(key, pages=None):
        """(entry, None), or (None, the hook's output: a deny or a pass-through)."""
        got = cache.lookup(key)
        if got is not None:
            return got, None
        fail = _failed(cache.root() / "failed" / key)
        if fail is not None:
            return None, _pass(path, fail)
        if engine != "native" and not verified:
            verified.append(True)
            if not pdfenv.verify(venv):
                raise _Unverified
        if command is not None:
            cmd = list(command)
        else:
            py = pdfenv.interpreter(venv) if engine != "native" else sys.executable
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
        if kind in DENY_KINDS:
            return None, _deny(path, kind, detail)
        why = _why(kind, detail)
        if not (kind == "timeout" and detail == LATE):  # the next Read has its full budget: let it try again
            _touch(cache.root() / "failed" / key, why)
        return None, _pass(path, why)

    if entry is None:
        entry, result = convert(k)
        if entry is None:
            return result
    if not (entry / "out.md").is_file():
        return _pass(path, _why("damaged", "cache entry vanished"))
    meta = _meta(entry)
    rng = parse_pages(event["tool_input"].get("pages"))
    total = meta.get("pages_total")
    past_end, at, drop = "", {}, ("pages",)  # fact F4a: updatedInput replaces the input, so pages can go
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
            if not index or b > max(p for p, _ in index):  # past the cut: convert that range alone, served whole
                entry, result = convert(f"{k}-p{a}-{b}", (a, b))
                if entry is None:
                    return result
                if not (entry / "out.md").is_file():
                    return _pass(path, _why("damaged", "cache entry vanished"))
                meta = _meta(entry)
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
    if engine == "native" and not pdfenv.forced_native() and not pdfenv.declined():
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            mark = cache.root() / "offers" / hashlib.sha1(sid.encode("utf-8", "replace")).hexdigest()
        if mark is None or not mark.exists():
            offer = offer_text(venv, repair)
    context = _context(_name(path), meta, out_md, past_end=past_end, offer=offer)
    if offer and mark is not None and offer in context:
        _touch(mark)
    return _allow(event, path, out_md, context, drop, **at)
