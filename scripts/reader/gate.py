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
            **dict.fromkeys(("xlsx", "xlsm", "xltx", "xltm"), "xlsx")}
EDIT_HINT = " Read-only view (no XML, cell addresses or formulas); to edit the file, use the {skill} skill."


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
    """True for a plain-text file (no NUL in the first 4 KiB, valid UTF-8, not RTF)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if b"\x00" in head:
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


def _deny(path, kind, detail=""):
    if kind == "damaged" and detail.startswith("internal error"):
        why = f"reading failed ({detail})"
    elif kind == "damaged":
        why = f"the file looks damaged ({detail})"
    elif kind == "too_large":
        why = TOO_LARGE.get(detail.split(":", 1)[0], REASONS["too_large"])
    elif kind == "timeout" and detail == LATE:
        why = LATE_TEXT
    else:
        why = REASONS.get(kind) or f"{kind} {detail}".strip()
    return deny_text(path, why, retry=kind in ("damaged", "timeout"))


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


def _context(name, meta, out_md):
    n = meta.get("lines", 0)
    m = meta.get("images", 0)
    k = meta.get("hidden", 0)
    head = f"{name} was converted to Markdown by phro-project ({n} line{'' if n == 1 else 's'}"
    if m:
        head += f", {m} image in assets/; Read it to view" if m == 1 else f", {m} images in assets/; Read them to view"
    head += ")."
    if k:
        head += f" {k} hidden places left out."
    skill = SKILL_OF.get(reader.ext_of(name))
    hint = EDIT_HINT.format(skill=skill) if skill else ""
    head += hint
    tail = ""
    if "garbled" in (meta.get("warnings") or []):
        tail = " Warning: the text may be garbled."
    unread = [" ".join(str(x).split()) for x in meta.get("unread") or []]
    if unread:
        prefix = " Some parts could not be read: "
        items = _fit(unread, CONTEXT_MAX - 1 - len(head) - len(tail) - len(prefix) - len(", …."))
        if items:
            head += prefix + ", ".join(items) + (", …." if len(items) < len(unread) else ".")
    long_out = n > INDEX_LINES
    if not long_out:
        try:
            if out_md.stat().st_size > INDEX_CHARS:
                long_out = len(out_md.read_text(encoding="utf-8", errors="replace")) > INDEX_CHARS
        except OSError:
            pass
    index = ""
    heads = meta.get("headings") or []
    if long_out and heads:
        prefix = " Sections at lines: "
        entries = [f'{line} "{t}"' for line, text in heads if (t := _heading(text))]
        items = _fit(entries, CONTEXT_MAX - 1 - len(head) - len(tail) - len(prefix) - len(", …"))
        if items:
            index = prefix + ", ".join(items) + (", …" if len(items) < len(entries) else "")
    return head + index + tail


def _now():
    import time  # not at module level: the gate's imports stay light for every Read
    return time.monotonic()


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
    from reader import cache
    k = cache.key(path)
    entry = cache.lookup(k)
    if entry is None:
        # hashing a big file on slow storage counts against the hook's 60 seconds too
        left = limits.HOOK_SECONDS - (_now() - start)
        if left < limits.HOOK_CHILD_MIN:
            return _deny(path, "timeout", LATE)
        budget = min(timeout, left)
        # the child hashes the bytes itself: a file saved after this hash must not be filed under k
        cmd = command or [sys.executable, "-I", "-X", "utf8", str(READ_PY), "convert", str(path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=budget)
        except subprocess.TimeoutExpired:
            return _deny(path, "timeout", LATE if budget < timeout else "")
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        if last.startswith("FAIL\t"):
            parts = last.split("\t", 2)
            return _deny(path, parts[1], parts[2] if len(parts) > 2 else "")
        out = Path(last) if last else None
        if r.returncode != 0 or out is None or out.name != "out.md" or not out.is_file():
            return _deny(path, "damaged", "conversion gave no output")
        entry = out.parent
    out_md = entry / "out.md"
    if not out_md.is_file():
        return _deny(path, "damaged", "cache entry vanished")
    try:
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    updated = dict(event["tool_input"])
    updated["file_path"] = str(out_md)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"phro-project converted {_name(path)} to Markdown",
        "updatedInput": updated,
        "additionalContext": _context(_name(path), meta, out_md),
    }}
