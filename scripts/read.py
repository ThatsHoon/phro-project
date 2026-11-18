"""CLI for the document reader: convert | setup | formats | prune | hook."""
import os
import sys

# This folder holds project CLIs named like stdlib modules (queue.py): it must come after the standard library
# on sys.path, and before site-packages, so an installed package named `reader` cannot take our place.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _place(paths: list, here: str) -> list:
    """`paths` without `here`, then `here` put just before the first site-packages entry (else last)."""
    key = os.path.normcase(here)
    rest = [p for p in paths if os.path.normcase(os.path.abspath(p or ".")) != key]
    for i, p in enumerate(rest):
        if os.path.basename(os.path.normpath(p)).lower() in ("site-packages", "dist-packages"):
            return rest[:i] + [here] + rest[i:]
    return rest + [here]


sys.path[:] = _place(sys.path, _HERE)

import argparse  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

READ_PY = Path(__file__).resolve()

import reader  # noqa: E402
from reader.limits import ReadError  # noqa: E402


def _flat(x) -> str:
    return " ".join(str(x).split())


def _is_key(k) -> bool:
    return isinstance(k, str) and len(k) == 40 and all(ch in "0123456789abcdef" for ch in k)


def _pages(text):
    """`A-B` with 1 <= A <= B, as (A, B)."""
    try:
        first, last = (int(x) for x in text.split("-"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected A-B, got {text!r}") from None
    if not 1 <= first <= last:
        raise argparse.ArgumentTypeError(f"expected A-B with 1 <= A <= B, got {text!r}")
    return first, last


def _engine_id(wanted) -> str:
    """The engine id a file is read with. pdf: for --engine pdfium, when pypdfium2 imports (the venv child),
    pyenv.engine_id_from_module(); otherwise native, so native output is never filed under a pdfium key. mpp: for
    --engine mpxj, pyenv.engine_id_from_module("mpp") when this is a ready mpp venv's interpreter, else "" (no engine:
    the file is unsupported)."""
    if wanted == "mpxj":
        try:
            from reader import pyenv
            return pyenv.engine_id_from_module("mpp")
        except Exception:  # noqa: BLE001 - not the mpp venv (no ready.json, no mpxj): no engine
            return ""
    if wanted != "pdfium":
        return "native"
    try:
        from reader import pyenv  # engine_id_from_module imports pypdfium2 itself
        return pyenv.engine_id_from_module()
    except Exception:  # noqa: BLE001 - not importable, or a broken install: read natively
        return "native"


_VENV_KIND = {"pdf": "pdf", "mpp": "mpp", "mpt": "mpp"}  # extension -> the venv that reads it
_VENV_ENGINE = {"pdf": "pdfium", "mpp": "mpxj"}


def _reexec(a):
    """A pdf (or an mpp or mpt) with no --engine, while a READY pdf (mpp) venv exists and this is not its
    interpreter: run the same convert there with --engine pdfium (mpxj) and relay its output. Returns the exit code,
    or None to convert in this process (pdf natively; mpp raises unsupported)."""
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
        r = subprocess.run(cmd, capture_output=True)
    except OSError:  # the venv interpreter cannot be launched: convert here (pdf natively; mpp is unsupported)
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


def cmd_convert(a) -> int:
    from reader import cache
    src = Path(a.file)
    code = _reexec(a)
    if code is not None:
        return code
    try:
        # a stray print in a format module must not reach the protocol stream
        with contextlib.redirect_stdout(sys.stderr):
            reader.precheck(src)
            opts, engine = {}, ""
            fmt = reader.FORMATS.get(reader.ext_of(src))
            if fmt == "pdf":
                engine = _engine_id("pdfium" if a.engine == "pdfium" else "native")
                opts["engine"] = "native" if engine == "native" else "pdfium"
                if a.pages:
                    opts["pages"] = a.pages
            elif fmt == "mpp" and a.engine == "mpxj":
                engine = _engine_id("mpxj")  # "" outside the mpp venv: no engine, so the file is unsupported
                if engine:
                    opts["engine"] = "mpxj"
            if a.out:
                out = Path(a.out)
                reader.convert(src, out, a.hidden, **opts)
            else:
                # a key handed in (--key) is used as it is
                k = a.key if _is_key(a.key) else cache.key(src, engine, _image_id(fmt))
                if "pages" in opts:
                    k += f"-p{a.pages[0]}-{a.pages[1]}"
                if a.hidden:
                    k += "-hidden"
                out = cache.lookup(k) or cache.store(k, src, a.hidden, **opts)
        print(out / "out.md")
    except ReadError as e:
        print(f"FAIL	{e.kind}	{_flat(e.detail)}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL	damaged	internal error {type(e).__name__}")  # the message may quote document text
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
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return 0
    try:
        out = gate.decide(event)
    except Exception as e:  # noqa: BLE001
        p = gate.scope(event)
        out = gate.deny_text(p, f"damaged: internal error {type(e).__name__}") if p else None
    if out is not None:
        print(json.dumps(out, ensure_ascii=False))
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
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
