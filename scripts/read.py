"""CLI for the document reader: convert | formats | prune | hook."""
import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    """The engine id a pdf is read with. Task 14 fills this: pdfium when asked for and its venv imports,
    with pdfenv.engine_id_from_module() as the id; until then the answer is always native."""
    return "native"


def cmd_convert(a) -> int:
    from reader import cache
    src = Path(a.file)
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
    sub.add_parser("formats").set_defaults(fn=cmd_formats)
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=30)
    pr.set_defaults(fn=cmd_prune)
    sub.add_parser("hook").set_defaults(fn=cmd_hook)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
