import os
import sys

# This folder holds project CLIs named like stdlib modules (queue.py): it must come after the standard library
# on sys.path, and before site-packages, so an installed package named `reader` cannot take our place.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _place(paths: list, here: str) -> list:
    key = os.path.normcase(here)
    rest = [p for p in paths if os.path.normcase(os.path.abspath(p or ".")) != key]
    for i, p in enumerate(rest):
        if os.path.basename(os.path.normpath(p)).lower() in ("site-packages", "dist-packages"):
            return rest[:i] + [here] + rest[i:]
    return rest + [here]


sys.path[:] = _place(sys.path, _HERE)

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX = frozenset("0123456789abcdefABCDEF")
_SPACE = " \t\n\r"


def _strings(s: str):
    """Every JSON string of s as (start, end, decoded text), found by the quotes alone; None when one is unfinished
    or holds an escape JSON refuses."""
    out, i = [], 0
    while (i := s.find('"', i)) >= 0:
        start, i, parts = i, i + 1, []
        while True:
            q, b = s.find('"', i), s.find("\\", i)
            if q < 0:
                return None
            if b < 0 or q < b:
                parts.append(s[i:q])
                i = q + 1
                break
            parts.append(s[i:b])
            c = s[b + 1:b + 2]
            if c == "u" and len(s[b + 2:b + 6]) == 4 and _HEX.issuperset(s[b + 2:b + 6]):
                parts.append(chr(int(s[b + 2:b + 6], 16)))
                i = b + 6
            elif c in _ESCAPES:
                parts.append(_ESCAPES[c])
                i = b + 2
            else:
                return None
        out.append((start, i, "".join(parts)))
    return out


def _suffix(path: str) -> str:
    """reader.ext_of(path) without pathlib: the last part's suffix as PurePath gives it, lower case, no dot."""
    name = os.path.basename(os.path.normpath(path)) if path else ""
    i = name.rfind(".")
    return name[i + 1:].lower() if 0 < i < len(name) - 1 else ""


def _not_a_document(raw: bytes) -> bool:
    """True when no "file_path" value of the hook event names an extension the reader takes: the Read goes ahead
    without the gate. Any doubt is False, and the gate decides."""
    s = raw.decode("utf-8", "replace")
    strings = _strings(s)
    if strings is None:
        return False
    exts = {}
    with open(os.path.join(_HERE, "reader", "exts.py"), encoding="utf-8") as f:
        exec(f.read(), exts)
    for n, (_, end, text) in enumerate(strings):
        j = end
        while j < len(s) and s[j] in _SPACE:
            j += 1
        if text != "file_path" or s[j:j + 1] != ":":
            continue
        j += 1
        while j < len(s) and s[j] in _SPACE:
            j += 1
        if n + 1 < len(strings) and strings[n + 1][0] == j and _suffix(strings[n + 1][2]) in exts["FORMATS"]:
            return False
    return True


_HOOK_INPUT = None
if __name__ == "__main__" and sys.argv[1:] == ["hook"]:
    _HOOK_INPUT = sys.stdin.buffer.read()
    if _not_a_document(_HOOK_INPUT):
        sys.exit(0)

import argparse
import contextlib
import json
from pathlib import Path

READ_PY = Path(__file__).resolve()

import reader
from reader.limits import CONVERT_SECONDS, HAND_SECONDS, ReadError


def _flat(x) -> str:
    return " ".join(str(x).split())


def _is_key(k) -> bool:
    return isinstance(k, str) and len(k) == 40 and all(ch in "0123456789abcdef" for ch in k)


def _pages(text):
    try:
        first, last = (int(x) for x in text.split("-"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected A-B, got {text!r}") from None
    if not 1 <= first <= last:
        raise argparse.ArgumentTypeError(f"expected A-B with 1 <= A <= B, got {text!r}")
    return first, last


def _engine_id(wanted) -> str:
    if wanted == "mpxj":
        try:
            from reader import pyenv
            return pyenv.engine_id_from_module("mpp")
        except Exception:
            return ""
    if wanted != "pdfium":
        return "native"
    try:
        from reader import pyenv
        return pyenv.engine_id_from_module()
    except Exception:
        return "native"


def _deadline_left() -> float:
    """Seconds to PHRO_READER_DEADLINE, at least 0; CONVERT_SECONDS when it is not a finite number."""
    import math
    import time
    try:
        wall = float(os.environ.get("PHRO_READER_DEADLINE", ""))
    except ValueError:
        wall = math.nan
    return max(0.0, wall - time.time()) if math.isfinite(wall) else float(CONVERT_SECONDS)


_VENV_KIND = {"pdf": "pdf", "mpp": "mpp", "mpt": "mpp"}
_VENV_ENGINE = {"pdf": "pdfium", "mpp": "mpxj"}


def source(arg: str) -> str:
    """The path `convert` was given: as it stands, or the path of a percent-encoded file: URI (the gate writes one
    for a path no quoting carries through both bash and PowerShell)."""
    if not arg.startswith("file:"):
        return arg
    from urllib.request import url2pathname
    try:
        return url2pathname(arg[5:])
    except OSError:
        return arg


def _reexec(a):
    kind = _VENV_KIND.get(reader.ext_of(Path(a.file)))
    if a.engine is not None or kind is None:
        return None
    from reader import pyenv
    if pyenv.forced_native(kind):
        return None
    venv = pyenv.venv_dir(kind)
    if pyenv.state(venv, kind) != "READY":
        return None
    py = pyenv.interpreter(venv)
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(py)):
        return None
    import subprocess
    cmd = [str(py), "-I", "-X", "utf8", str(READ_PY), "convert", a.file]
    if a.out:
        cmd += ["--out", a.out]
    if a.hidden:
        cmd.append("--hidden")
    if a.key:
        cmd += ["--key", a.key]
    if a.pages and kind == "pdf":
        cmd += ["--pages", f"{a.pages[0]}-{a.pages[1]}"]
    cmd += ["--engine", _VENV_ENGINE[kind]]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_deadline_left())
    except subprocess.TimeoutExpired:
        sys.stdout.write("FAIL\ttimeout\t\n")
        return 1
    except OSError:
        return None
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    if r.stderr:
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
    return r.returncode


def cmd_setup(a) -> int:
    from reader import pyenv, pysetup
    kind = a.kind
    venv = Path(a.venv) if a.venv else pyenv.venv_dir(kind)
    if a.check:
        line = pysetup.check(venv, kind)
    elif a.decline:
        line = pysetup.decline(kind)
    elif a.remove:
        line = pysetup.remove(venv, kind)
    else:
        line = pysetup.setup(venv, kind=kind)
    print(line)
    return 1 if line.startswith("FAIL") else 0


def _image_id(fmt) -> str:
    """The figure backend id of the cache key for format module `fmt`: none for pdf and mpp (their venv children
    have no figure backend: the engine id alone). On Windows the gate's own id (gate.image_backend), so GDI+ is not
    started just for a key and the child files the read where the gate looks even when GdiplusStartup fails here;
    elsewhere the image venv's id as this process works it out (raster.backend)."""
    if fmt in ("pdf", "mpp"):
        return ""
    if sys.platform == "win32":
        from reader import gate
        return gate.image_backend()
    from reader import raster
    return raster.backend()


def _ocr_id(fmt) -> tuple[str, str]:
    """The OCR backend (id, version) for format module `fmt`: the gate's own probe (ocr_probe.backend), so the child
    files the read where the gate looks; none for mpp, which has no pictures. Worked out once: the key carries it
    (gate.ocr_key) and reader.convert opens its OCR session with it."""
    if fmt == "mpp":
        return "", ""
    from reader import ocr_probe
    return ocr_probe.backend()


def cmd_convert(a) -> int:
    from reader import cache
    if not os.environ.get("PHRO_READER_DEADLINE"):
        import time
        os.environ["PHRO_READER_DEADLINE"] = repr(time.time() + HAND_SECONDS)
    a.file = source(a.file)
    src = Path(a.file)
    code = _reexec(a)
    if code is not None:
        return code
    try:
        with contextlib.redirect_stdout(sys.stderr):
            reader.precheck(src)
            opts, engine = {}, ""
            fmt = reader.FORMATS.get(reader.ext_of(src))
            opts["backend"] = _ocr_id(fmt)
            if fmt == "pdf":
                engine = _engine_id("pdfium" if a.engine == "pdfium" else "native")
                opts["engine"] = "native" if engine == "native" else "pdfium"
                if a.pages:
                    opts["pages"] = a.pages
            elif fmt == "mpp" and a.engine == "mpxj":
                engine = _engine_id("mpxj")
                if engine:
                    opts["engine"] = "mpxj"
            if a.out:
                out = Path(a.out)
                reader.convert(src, out, a.hidden, **opts)
            else:
                if _is_key(a.key):
                    k = a.key
                else:
                    from reader import gate
                    k = cache.key(src, engine, _image_id(fmt), gate.ocr_key(opts["backend"]))
                if "pages" in opts:
                    k += f"-p{a.pages[0]}-{a.pages[1]}"
                if a.hidden:
                    k += "-hidden"
                out = cache.lookup(k)
                if out is None or cache.partial(out):
                    out = cache.store(k, src, a.hidden, again=out is not None, **opts)
        print(out / "out.md")
    except ReadError as e:
        print(f"FAIL	{e.kind}	{_flat(e.detail)}")
        return 1
    except Exception as e:
        print(f"FAIL	damaged	internal error {type(e).__name__}")
        return 1
    return 0


def cmd_formats(a) -> int:
    for ext in sorted(reader.FORMATS):
        state = "built" if ext in reader.BUILT else "unsupported"
        print(f"{ext}\t{reader.FORMATS[ext]}\t{state}")
    return 0


def cmd_prune(a) -> int:
    from reader import cache
    print(f"PRUNED\t{cache.prune(a.days)}")
    return 0


def cmd_hook(a) -> int:
    from reader import gate
    try:
        raw = sys.stdin.buffer.read() if _HOOK_INPUT is None else _HOOK_INPUT
        event = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return 0
    try:
        out = gate.decide(event)
    except Exception as e:
        p = gate.scope(event)
        out = gate._deny(p, "damaged", f"internal error {type(e).__name__}") if p else None
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_ocr_worker(a) -> int:
    """The OCR worker (reader.ocr_worker), started by ocr.Session: frames on stdin and stdout. Anything printed
    goes to stderr, so no stray line lands between frames."""
    from reader import ocr_worker
    out = sys.stdout.buffer
    sys.stdout.flush()
    sys.stdout = sys.stderr
    ocr_worker.serve(sys.stdin.buffer, out)
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="read.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("file")
    c.add_argument("--out")
    c.add_argument("--hidden", action="store_true")
    c.add_argument("--key", help=argparse.SUPPRESS)
    c.add_argument("--engine", choices=("pdfium", "native", "mpxj"))
    c.add_argument("--pages", type=_pages, metavar="A-B")
    c.set_defaults(fn=cmd_convert)
    s = sub.add_parser("setup")
    s.add_argument("--for", dest="kind", choices=("pdf", "mpp", "image"), default="pdf")
    s.add_argument("--venv")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--decline", action="store_true")
    mode.add_argument("--remove", action="store_true")
    s.set_defaults(fn=cmd_setup)
    sub.add_parser("formats").set_defaults(fn=cmd_formats)
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=30)
    pr.set_defaults(fn=cmd_prune)
    sub.add_parser("hook").set_defaults(fn=cmd_hook)
    sub.add_parser("ocr-worker", help=argparse.SUPPRESS).set_defaults(fn=cmd_ocr_worker)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
