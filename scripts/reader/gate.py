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
    "too_large": "the file is over 512 MiB",
    "timeout": f"conversion took over {limits.CONVERT_SECONDS} seconds",
}


def scope(event):
    """The document path when this event is a Read of an in-scope file, else None."""
    if not isinstance(event, dict) or event.get("tool_name") != "Read":
        return None
    ti = event.get("tool_input")
    fp = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(fp, str) or not fp:
        return None
    if reader.ext_of(fp) not in reader.FORMATS:
        return None
    p = Path(fp)
    try:
        return p if p.is_file() else None
    except OSError:
        return None


def deny_text(path, why):
    cmd = f'python -I -X utf8 "{READ_PY}" convert "{path}"'
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{path.name}: {why}. Retry by hand: {cmd}",
    }}


def _deny(path, kind, detail=""):
    if kind == "damaged":
        why = f"the file looks damaged ({detail})"
    else:
        why = REASONS.get(kind) or f"{kind} {detail}".strip()
    return deny_text(path, why)


def _context(name, meta, out_md):
    n = meta.get("lines", 0)
    m = meta.get("images", 0)
    k = meta.get("hidden", 0)
    head = f"{name} was converted to Markdown by phro-project ({n} lines"
    if m:
        head += f", {m} images in assets/; Read them to view"
    head += ")."
    if k:
        head += f" {k} hidden places left out."
    tail = ""
    if "garbled" in (meta.get("warnings") or []):
        tail = " Warning: the text may be garbled."
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
        room = CONTEXT_MAX - 1 - len(head) - len(tail) - len(prefix) - len(", …")
        items, used = [], 0
        for line, text in heads:
            item = f"{line} {' '.join(str(text).split())[:60]}"
            cost = len(item) + (2 if items else 0)
            if used + cost > room:
                break
            items.append(item)
            used += cost
        if items:
            index = prefix + ", ".join(items) + (", …" if len(items) < len(heads) else "")
    return head + index + tail


def decide(event, *, timeout=limits.CONVERT_SECONDS, command=None):
    path = scope(event)
    if path is None:
        return None
    if reader.ext_of(path) not in reader.BUILT:
        return _deny(path, "unsupported")
    if path.stat().st_size > limits.MAX_INPUT:
        return _deny(path, "too_large")
    from reader import cache
    k = cache.key(path)
    entry = cache.lookup(k)
    if entry is None:
        cmd = command or [sys.executable, "-I", "-X", "utf8", str(READ_PY), "convert", str(path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return _deny(path, "timeout")
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
    try:
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    updated = dict(event["tool_input"])
    updated["file_path"] = str(out_md)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"phro-project converted {path.name} to Markdown",
        "updatedInput": updated,
        "additionalContext": _context(path.name, meta, out_md),
    }}
