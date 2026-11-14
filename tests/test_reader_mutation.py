"""Fixed-seed mutation tests: synthetic fixtures of xls (clear, RC4, XOR), xlsb, ppt (clear, encrypted) and rtf are
damaged at random and read; every outcome must be a Doc or a ReadError (no other exception, no hang).

About 100 cases a format, each from `random.Random(SEED + k)`, so a failure names the case and repeats. No damaged
case is ever written to disk: compound files and zips are built in memory and read through the formats' in-memory
entries (`xls.parse_compound`, `ppt.parse_compound`, `xlsb.parse_package`, `rtf.parse_bytes`), and rendering never
writes an image. Only the clean xlsb and ppt seeds touch a temporary directory, once each. The RTF seed holds none of
the control words of a shape's property groups, and the test builds them with `cat` so no .pyc holds them either.

Run: python -I -X utf8 tests/test_reader_mutation.py
"""
import random
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.containers.officecrypt import PPT_DEFAULT  # noqa: E402
from reader.formats import ppt, rtf, xls, xlsb  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context, Doc  # noqa: E402
from reader_fix import blip_png, cfb_bytes, encrypt_xls, make_ppt, make_xlsb, make_zip  # noqa: E402

SEED = 20261114
CASES = 100
SLOW = 5.0  # seconds one case may take: a case past it is a hang

SHEETS = [{"name": "First", "cells": {(0, 0): "alpha", (0, 1): 42.5, (1, 0): "한글 text", (2, 2): 7}},
          {"name": "Second", "cells": {(0, 0): "beta " * 30, (1, 1): "gamma"}}]
SLIDES = [{"title": "Quarterly plan", "body": ["first point", "second point"]},
          {"title": "Second slide", "body": ["more text"]}]
PNG = b"\x89PNG\r\n\x1a\n" + b"P" * 24


def cat(*parts):
    """The parts joined at call time: a concatenation of constants is folded by the compiler and lands in the .pyc."""
    return b"".join(parts)


# ---- mutations
def plan(rng, size):
    """A list of edits for data of `size` bytes, chosen only from rng and size so that the same plan can damage two
    streams of one length (a clear stream and its encryption) at the same places."""
    edits = []
    for _ in range(rng.choice((1, 1, 2, 3, 6))):
        op = rng.choice(("flip", "flip", "set", "run", "cut", "drop", "dup", "word"))
        at = rng.randrange(max(size, 1))
        if op == "flip":
            edits.append((op, at, rng.randrange(1, 256)))
        elif op == "set":
            edits.append((op, at, rng.choice((0x00, 0xFF, 0x7F, 0x80))))
        elif op == "run":
            edits.append((op, at, rng.choice((0x00, 0xFF)), rng.randrange(2, 40)))
        elif op == "cut":
            edits.append((op, at))
        elif op == "drop":
            edits.append((op, at, rng.randrange(1, 64)))
        elif op == "dup":
            edits.append((op, at, rng.randrange(1, 64)))
        else:
            edits.append((op, at, rng.choice((0xFFFF, 0x7FFF, 0x8000, 0x0001, 0))))
    return edits


def apply(edits, data):
    out = bytearray(data)
    for e in edits:
        op, at = e[0], min(e[1], max(len(out) - 1, 0))
        if not out:
            break
        if op == "flip":
            out[at] ^= e[2]
        elif op == "set":
            out[at] = e[2]
        elif op == "run":
            out[at:at + e[3]] = bytes([e[2]]) * min(e[3], len(out) - at)
        elif op == "cut":
            del out[at:]
        elif op == "drop":
            del out[at:at + e[2]]
        elif op == "dup":
            out[at:at] = out[at:at + e[2]]
        else:
            out[at:at + 2] = e[2].to_bytes(2, "little")
    return bytes(out)


# ---- the harness
class MemAssets:
    """Stands in for images.Assets while a case is rendered: no image is ever written out."""

    def __init__(self, dir):
        pass

    def add(self, data, alt=""):
        return "assets/mem.png", True


def render(doc, skipped):
    real, markdown.Assets = markdown.Assets, MemAssets
    try:
        return markdown.render(doc, Path("assets-never-written"), False, skipped)
    finally:
        markdown.Assets = real


def outcome(read, label):
    """"doc", "routed" (another parser's compound file) or the ReadError kind; any other exception fails with the
    case's label, a case past SLOW is a hang."""
    t0 = time.monotonic()
    try:
        got = read()
        kind = "doc" if isinstance(got, Doc) else "routed" if isinstance(got, str) else None
        assert kind, f"{label}: returned {type(got).__name__}, not a Doc"
    except ReadError as e:
        kind = e.kind
        assert kind in ("damaged", "encrypted", "unsupported", "too_large"), f"{label}: ReadError kind {kind!r}"
    except AssertionError:
        raise
    except BaseException as e:
        raise AssertionError(f"{label}: {type(e).__name__}: {e}") from e
    took = time.monotonic() - t0
    assert took < SLOW, f"{label}: {took:.1f}s"
    return kind


def run_cases(label, make_case, cases=CASES, seed=SEED):
    """Run `cases` cases; make_case(rng, k) returns a callable that reads the damaged input from memory. Returns the
    outcome counts. Nothing damaged is written to disk."""
    counts = {}
    for k in range(cases):
        rng = random.Random(seed + k)
        kind = outcome(make_case(rng, k), f"{label} case {k} (seed {seed + k})")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def read_doc(parse, data):
    ctx = Context()
    doc = parse(data, ctx)
    if isinstance(doc, Doc):
        render(doc, ctx.skipped)
    return doc


# ---- xls
def encrypt_stream(kind):
    from reader_fix import biff_workbook
    if kind is None:
        return biff_workbook(SHEETS)
    return encrypt_xls(SHEETS, "VelvetSweatshop", kind)


def xls_streams():
    return {"clear": encrypt_stream(None), "rc4": encrypt_stream("rc4"), "xor": encrypt_stream("xor")}


def xls_case(streams, kind):
    """Cases that damage `kind`'s Workbook stream. A case's plan is drawn from its seed and the clear stream's length
    alone, so the three kinds are damaged at the same places (the encrypted streams are a FILEPASS record longer)."""
    def make(rng, k):
        edits = plan(rng, len(streams["clear"]))
        blob = cfb_bytes({"Workbook": apply(edits, streams[kind])})[0]
        return lambda: read_doc(xls.parse_compound, blob)
    return make


def test_xls_mutations():
    streams = xls_streams()
    for kind in ("clear", "rc4", "xor"):
        counts = run_cases(f"xls {kind}", xls_case(streams, kind))
        assert sum(counts.values()) == CASES, counts
    # The same plans on the clear stream and on its RC4 and XOR encryptions (record headers stay clear):
    # the shares of framing refusals ("damaged") agree. The streams differ only in what a damaged body decrypts to, so
    # a body edit rarely changes whether the records still frame; the margin is 0.15, about 3 standard errors of a
    # difference of two shares of 100 near one half (sqrt(2 * .25 / 100) = .07), not a tuned value.
    shares = {}
    for kind in ("clear", "rc4", "xor"):
        counts = run_cases(f"xls {kind} paired", xls_case(streams, kind), seed=SEED + 1000)
        shares[kind] = counts.get("damaged", 0) / CASES
    for kind in ("rc4", "xor"):
        assert abs(shares["clear"] - shares[kind]) <= 0.15, shares


# ---- xlsb (the clean seed is built once in a temporary directory and read back; no damaged case is)
def xlsb_members():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.xlsb"
        make_xlsb(path, SHEETS)
        with zipfile.ZipFile(path) as z:
            return {i.filename: z.read(i.filename) for i in z.infolist()}


def test_xlsb_mutations():
    members = xlsb_members()
    binary = sorted(n for n in members if n.endswith(".bin"))
    assert binary, members.keys()

    def make(rng, k):
        parts = dict(members)
        for name in rng.sample(binary, rng.choice((1, 1, 2))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        blob = make_zip(None, parts, stored=())
        return lambda: read_doc(xlsb.parse_package, blob)

    counts = run_cases("xlsb", make)
    assert sum(counts.values()) == CASES, counts


# ---- ppt
def ppt_streams(**kw):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "seed.ppt"
        make_ppt(path, SLIDES, notes={1: "speaker words"}, pictures=[blip_png(PNG)], **kw)
        with Compound(path) as cf:
            return {n: cf.read(n) for n in ("PowerPoint Document", "Current User", "Pictures")}


def ppt_case(streams):
    names = ("PowerPoint Document", "PowerPoint Document", "Pictures", "Current User")

    def make(rng, k):
        parts = dict(streams)
        for name in rng.sample(names, rng.choice((1, 1, 2))):
            parts[name] = apply(plan(rng, len(parts[name])), parts[name])
        blob = cfb_bytes(parts)[0]
        return lambda: read_doc(ppt.parse_compound, blob)
    return make


def test_ppt_mutations():
    for label, kw in (("clear", {}), ("encrypted", {"encrypt": PPT_DEFAULT})):
        counts = run_cases(f"ppt {label}", ppt_case(ppt_streams(**kw)))
        assert sum(counts.values()) == CASES, counts


# ---- rtf: the seed holds no shape control words, and nothing is written out
RTF_SEED = (
    b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fcharset0 Arial;}{\\f1\\fcharset128 MS Mincho;}}"
    b"{\\colortbl;\\red0\\green0\\blue0;}{\\stylesheet{\\s1 heading 1;}{\\s2 heading 2;}{\\s3\\sbasedon2 sub;}}"
    b"{\\info{\\title Seed title}{\\author Someone}}{\\header\\pard Top line\\par}{\\footer\\pard Bottom\\par}"
    b"\\pard\\s1 Heading one\\par\\pard\\plain\\f0\\fs24 Plain {\\b bold \\i italic} \\'e9\\'e8 \\u8212? "
    b"\\u-10179?\\u-8704? {\\v hidden words}\\line next\\par"
    b"\\pard\\outlinelevel2 Outline two\\par\\pard\\ls1\\ilvl1 {\\listtext 1.\\tab}list item\\par"
    b"\\trowd\\trleft-108\\cellx1000\\cellx2000\\pard\\intbl a\\cell b\\cell\\row"
    b"\\trowd\\clmgf\\cellx1000\\clmrg\\cellx2000\\pard\\intbl c\\cell \\cell\\row\\pard after table\\par"
    b"\\pard note{\\footnote\\pard\\plain note text}{\\footnote\\ftnalt end}\\par"
    b"{\\field{\\*\\fldinst HYPERLINK \"http://example.test/a\"}{\\fldrslt link text}}\\par"
    b"{\\*\\shppict{\\pict\\pngblip\\picw1\\pich1 89504e470d0a1a0a0000000000000000000000000000000000000000}}"
    b"{\\object{\\*\\objdata 0102}{\\result object result}}"
    b"{\\pict\\dibitmap0\\bin16 0123456789abcdef}\\pard last\\par}"
)


def test_rtf_seed_has_no_shape_words():
    for word in (cat(b"\\s", b"p"), cat(b"\\s", b"n"), cat(b"\\s", b"v"), cat(b"\\sh", b"pinst"),
                 cat(b"pFra", b"gments")):
        # the control words: a letter or a delimiter must follow, `\sbasedon` and `\s1` are not them
        assert word not in RTF_SEED.replace(b"\\sbasedon", b"").replace(b"\\s1", b"").replace(b"\\s2", b"") \
            .replace(b"\\s3", b""), word


def test_rtf_mutations():
    doc = rtf.parse_bytes(RTF_SEED, Context())
    assert "Heading one" in render(doc, {})

    def make(rng, k):
        data = apply(plan(rng, len(RTF_SEED)), RTF_SEED)
        if rng.random() < 0.3:  # also brace and backslash noise: the bytes that steer the lexer
            at = rng.randrange(len(data))
            data = data[:at] + rng.choice((b"{", b"}", b"\\", b"\\bin9999 ", b"\\u", b"\\'", b"\\*")) + data[at:]

        return lambda: read_doc(rtf.parse_bytes, data)

    counts = run_cases("rtf", make)
    assert sum(counts.values()) == CASES, counts


def main():
    t0 = time.monotonic()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            t1 = time.monotonic()
            fn()
            print(f"{name}: {time.monotonic() - t1:.2f}s")
    print(f"mutation tests total: {time.monotonic() - t0:.2f}s")
    print("pass")


if __name__ == "__main__":
    main()
