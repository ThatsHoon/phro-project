"""CLI for the document reader: convert | formats | prune."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import cache  # noqa: E402
from reader.limits import ReadError  # noqa: E402


def cmd_convert(a) -> int:
    src = Path(a.file)
    try:
        if a.out:
            out = Path(a.out)
            reader.convert(src, out, a.hidden)
        else:
            k = cache.key(src)
            if a.hidden:
                k += "-hidden"
            out = cache.lookup(k) or cache.store(k, src, a.hidden)
    except ReadError as e:
        print(f"FAIL\t{e.kind}\t{e.detail}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"FAIL\tdamaged\t{type(e).__name__}: {e}")
        return 1
    print(out / "out.md")
    return 0


def cmd_formats(a) -> int:
    for ext in sorted(reader.FORMATS):
        state = "built" if ext in reader.BUILT else "unsupported"
        print(f"{ext}\t{reader.FORMATS[ext]}\t{state}")
    return 0


def cmd_prune(a) -> int:
    print(f"PRUNED\t{cache.prune(a.days)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="read.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("file")
    c.add_argument("--out")
    c.add_argument("--hidden", action="store_true")
    c.set_defaults(fn=cmd_convert)
    sub.add_parser("formats").set_defaults(fn=cmd_formats)
    pr = sub.add_parser("prune")
    pr.add_argument("--days", type=int, default=30)
    pr.set_defaults(fn=cmd_prune)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
