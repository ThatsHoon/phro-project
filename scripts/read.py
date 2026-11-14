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
    """The engine id a pdf is read with: for --engine pdfium, when pypdfium2 imports (the venv child),
    pdfenv.engine_id_from_module(); otherwise native, so native output is never filed under a pdfium key."""
    if wanted != "pdfium":
        return "native"
    try:
        from reader import pdfenv  # engine_id_from_module imports pypdfium2 itself
        return pdfenv.engine_id_from_module()
    except Exception:  # noqa: BLE001 - not importable, or a broken install: read natively
        return "native"


def _reexec(a):
    """A pdf with no --engine, while a READY pdf venv exists and this is not its interpreter: run the same convert
    there with --engine pdfium and relay its output. Returns the exit code, or None to read natively."""
    if a.engine is not None or reader.ext_of(Path(a.file)) != "pdf":
        return None
    from reader import pdfenv
    if pdfenv.forced_native():
        return None
    venv = pdfenv.venv_dir()
    if pdfenv.state(venv) != "READY":
        return None
    py = pdfenv.interpreter(venv)
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
    if a.pages:
        cmd += ["--pages", f"{a.pages[0]}-{a.pages[1]}"]
    cmd += ["--engine", "pdfium"]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except OSError:  # the venv interpreter cannot be launched: read natively
        return None
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    if r.stderr:
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
    return r.returncode


def cmd_setup(a) -> int:
    from reader import pdfenv, pdfsetup
    venv = Path(a.venv) if a.venv else pdfenv.venv_dir()
    if a.check:
        line = pdfsetup.check(venv)
    elif a.decline:
        line = pdfsetup.decline()
    elif a.remove:
        line = pdfsetup.remove(venv)
    else:
        line = pdfsetup.setup(venv)
    print(line)
    return 1 if line.startswith("FAIL") else 0


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
            if reader.ext_of(src) == "pdf":
                engine = _engine_id(a.engine)
                opts["engine"] = "native" if engine == "native" else "pdfium"
                if a.pages:
                    opts["pages"] = a.pages
            if a.out:
                out = Path(a.out)
                reader.convert(src, out, a.hidden, **opts)
            else:
                k = a.key if _is_key(a.key) else cache.key(src, engine)  # the gate passes the key it already worked out
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
    c.add_argument("--engine", choices=("pdfium", "native"))
    c.add_argument("--pages", type=_pages, metavar="A-B")
    c.set_defaults(fn=cmd_convert)
    s = sub.add_parser("setup")
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
