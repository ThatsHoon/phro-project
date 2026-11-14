"""ppt: slides, outline and text-box text, bullets, bold runs, notes, hidden slides, damaged slides, renamed files.

Run: python -I -X utf8 tests/test_reader_ppt.py
"""
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import markdown  # noqa: E402
from reader.formats import ppt_text  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Context  # noqa: E402
from reader_fix import make_cfb, make_ppt  # noqa: E402


def build(slides, **kw):
    """Build a .ppt in a fresh folder: (folder, path, make_ppt's info)."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    return d, path, make_ppt(path, slides, **kw)


def render(d, path, hidden=False):
    ctx = Context()
    doc = reader.parse(path, ctx)
    return markdown.render(doc, d / "assets", hidden, ctx.skipped), ctx


def md(slides, hidden=False, **kw):
    """Build a .ppt, parse it, render it: (markdown, ctx)."""
    d, path, _ = build(slides, **kw)
    return render(d, path, hidden)


def rewrite(path, info, data):
    make_cfb(path, {"PowerPoint Document": bytes(data), "Current User": info["current_user"], "Pictures": b""})


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def test_titles_and_body():
    for outline in (False, True):
        out, ctx = md([{"title": "Intro", "body": ["first line", "second line"], "outline": outline},
                       {"title": "Plan", "body": ["step one"], "outline": outline}])
        assert "## Slide 1: Intro" in out and "## Slide 2: Plan" in out, out
        assert "first line" in out and "second line" in out and "step one" in out, out
        assert out.index("Intro") < out.index("first line") < out.index("Plan") < out.index("step one"), out
        assert not ctx.skipped, ctx.skipped
        for line in ("Intro", "Plan", "first line", "second line", "step one"):
            assert out.count(line) == 1, (outline, line, out)


def test_slide_text_without_drawing():
    out, _ = md([{"title": "First", "body": ["alpha"], "no_drawing": True}, {"body": ["beta"], "no_drawing": True}])
    assert "## Slide 1: First" in out and "alpha" in out, out
    assert "## Slide 2" in out and "beta" in out, out
    out, _ = md([{"title": "One", "no_drawing": True}], notes={1: "spoken"})
    assert "> Notes: spoken" in out, out


def test_slide_text_no_drawing_with_outline_once():
    out, _ = md([{"title": "Heading", "body": ["bodyline"], "no_drawing": True, "outline": True}])
    assert out.count("Heading") == 1 and out.count("bodyline") == 1, out


def test_slide_text_no_drawing_outline_differs_kept():
    # the slide list holds a run the container's own atoms do not: it is kept, not dropped
    out, _ = md([{"title": "Heading", "body": ["bodyline"], "no_drawing": True, "outline": True,
                  "extra": [(1, "only in the list")]}])
    assert out.count("Heading") == 1 and out.count("bodyline") == 1 and "only in the list" in out, out


def test_notes():
    out, _ = md([{"title": "Intro", "body": ["text"]}, {"title": "Plan"}], notes={1: "speak slowly"})
    assert "> Notes: speak slowly" in out, out
    assert out.index("> Notes") < out.index("## Slide 2"), out


def test_notes_second_slide_only():
    out, _ = md([{"title": "One"}, {"title": "Two"}], notes={2: "only here"})
    assert out.count("only here") == 1 and out.index("## Slide 2") < out.index("only here"), out


def prun(count, indent=0, mask=0, extra=b""):
    return struct.pack("<IHI", count, indent, mask) + extra


def crun(count, mask=0, extra=b""):
    return struct.pack("<II", count, mask) + extra


def texts(blocks):
    return [(type(b).__name__, [type(i).__name__ + ":" + getattr(i, "text", "") for i in b.inlines]) for b in blocks]


def test_control_char_keeps_bold_boundary():
    # raw text a, control, b, c: the bold run covers the control character and b
    style = prun(5) + crun(1) + crun(2, 1, struct.pack("<H", 1)) + crun(2)
    got = ppt_text.blocks("a\x01bc", style, 1, Context())
    assert texts(got) == [("Para", ["Text:a", "Bold:", "Text:c"])], texts(got)
    assert got[0].inlines[1].inlines[0].text == "b", got


def test_control_char_keeps_bullet_level():
    # paragraph 1 is "a\x01" + "b" (4 chars with its end), paragraph 2 "cd" is bulleted at level 1
    style = prun(4) + prun(3, 1, 1, struct.pack("<H", 1)) + crun(7)
    got = ppt_text.blocks("a\x01b\rcd", style, 1, Context())
    assert [type(b).__name__ for b in got] == ["Para", "ListItem"] and got[1].level == 1, got


def test_zero_run_count_plain():
    ctx = Context()
    got = ppt_text.blocks("ab", prun(0) + crun(3), 1, ctx)
    assert texts(got) == [("Para", ["Text:ab"])] and ctx.skipped["record"] == 1, (got, ctx.skipped)


def test_too_many_runs_plain():
    ctx = Context()
    style = prun(1) * 100_001
    got = ppt_text.blocks("a" * 100_002, style, 1, ctx)
    assert len(got) == 1 and type(got[0]).__name__ == "Para" and ctx.skipped["record"] == 1, ctx.skipped


def test_run_count_past_text_clamped():
    ctx = Context()
    style = prun(1000, 0, 1, struct.pack("<H", 1)) + crun(1000, 1, struct.pack("<H", 1))
    got = ppt_text.blocks("abc", style, 1, ctx)
    assert len(got) == 1 and type(got[0]).__name__ == "ListItem" and not ctx.skipped, (got, ctx.skipped)
    assert type(got[0].inlines[0]).__name__ == "Bold" and got[0].inlines[0].inlines[0].text == "abc", got


def test_hidden_slide():
    slides = [{"title": "Intro", "body": ["shown text"]}, {"title": "Plan", "body": ["secret text"], "hidden": True}]
    out, _ = md(slides)
    assert "(1 hidden places left out)" in out and "secret text" not in out and "Plan" not in out, out
    out, _ = md(slides, hidden=True)
    assert "## Slide 2: Plan (hidden)" in out and "secret text" in out, out


def test_bullets_levels():
    for outline in (False, True):
        out, _ = md([{"title": "T", "body": [("top", 0), ("nested", 1), "plain"], "outline": outline}])
        lines = out.splitlines()
        assert "- top" in lines and "  - nested" in lines and "plain" in lines, out


def test_bold_run():
    out, _ = md([{"title": "T", "body": ["the key point"], "bold": ["key"]}])
    assert "the **key** point" in out, out


def test_text_bytes():
    out, _ = md([{"title": "Caf\xe9 menu", "text_bytes": True}])
    assert "## Slide 1: Caf\xe9 menu" in out, out


def test_line_break():
    out, _ = md([{"title": "T", "body": ["a\x0bb"]}])
    body = out.split("## Slide 1: T", 1)[1]
    assert "ab" not in body and "a" in body and "b" in body, out
    assert body.strip().count("\n") >= 1 or "<br>" in body, out


def test_master_text_quiet():
    out, _ = md([{"title": "T", "body": ["x"]}], master_text="Click to edit")
    assert "Click to edit" not in out, out


def test_master_without_text():
    out, ctx = md([{"title": "T", "body": ["x"]}], master_text=None)
    assert "## Slide 1: T" in out and "x" in out and not ctx.skipped, (out, ctx.skipped)


def test_unknown_text_header_type_is_body_text():
    # TextHeaderAtom types are 0..8; any other value is read as body text: never a title, never dropped
    out, ctx = md([{"title": "Head", "body": ["real body"], "outline": True, "extra": [(99, "odd words")]},
                   {"body": ["only"], "outline": True, "extra": [(0xFFFF, "stray")]}])
    first, second = out.split("## Slide 2", 1)
    assert "## Slide 1: Head" in first and "real body" in first and "odd words" in first, out
    assert first.index("real body") < first.index("odd words"), out
    assert "only" in second and "stray" in second and "## Slide 2:" not in out, out
    assert not ctx.skipped, ctx.skipped


def test_bad_slide_counted():
    d, path, info = build([{"title": "One"}, {"title": "Two"}, {"title": "Three"}])
    data = bytearray(info["doc_stream"])
    struct.pack_into("<I", data, info["slots"][info["slides"][1]], len(data) + 1000)
    rewrite(path, info, data)
    out, ctx = render(d, path)
    assert "## Slide 1: One" in out and "## Slide 3: Three" in out and "Two" not in out, out
    assert "(could not read: 1 slide)" in out and ctx.skipped["slide"] == 1, (out, ctx.skipped)


def test_no_slides_damaged():
    d, path, info = build([{"title": "One"}, {"title": "Two"}])
    data = bytearray(info["doc_stream"])
    for pid in info["slides"]:
        struct.pack_into("<I", data, info["slots"][pid], len(data) + 1000)
    rewrite(path, info, data)
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("damaged", "no readable slides"), got


def test_cut_stream_reads_slides_before_the_cut():
    """A Document stream cut inside slide 2 keeps no persist directory: the top-level records are walked instead,
    slide 1 reads and the broken chain is counted."""
    d, path, info = build([{"title": "One", "body": ["first body"]}, {"title": "Two", "body": ["second body"]}])
    cut = info["offsets"][info["slides"][1]] + 20
    rewrite(path, info, info["doc_stream"][:cut])
    out, ctx = render(d, path)
    assert "## Slide 1: One" in out and "first body" in out and "Two" not in out, out
    assert "(could not read: 1 damaged record)" in out and dict(ctx.skipped) == {"record": 1}, (out, ctx.skipped)


def test_lost_directory_reads_every_slide():
    """Only the persist directory and the edit are lost: every slide reads in stream order and the notes, which the
    chain alone ties to their slides, are counted."""
    d, path, info = build([{"title": "One", "body": ["a"]}, {"title": "Two", "body": ["b"], "hidden": True}],
                          notes={1: "the note"})
    rewrite(path, info, info["doc_stream"][:info["directories"][0]])
    out, ctx = render(d, path, hidden=True)
    assert "## Slide 1: One" in out and "Slide 2: Two" in out and "the note" not in out, out
    assert dict(ctx.skipped) == {"record": 1, "note": 1}, ctx.skipped


def test_cut_stream_without_slides_damaged():
    d, path, info = build([{"title": "One"}])
    rewrite(path, info, info["doc_stream"][:info["offsets"][info["slides"][0]]])
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("damaged", "no persist directory"), got
    # an encrypted stream needs its chain: no records are walked
    d, path, info = build([{"title": "One"}], encrypt="/01Hannes Ruescher/01")
    rewrite(path, info, info["doc_stream"][:info["directories"][0]])
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("damaged", "no persist directory"), got


def test_encrypted_package_named_ppt():
    """A password-protected pptx (EncryptionInfo + EncryptedPackage) renamed .ppt is encrypted, not damaged."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_cfb(path, {"EncryptionInfo": b"\x04\x00\x04\x00" + bytes(60), "EncryptedPackage": bytes(4096)})
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("encrypted", ""), got


def test_xls_named_ppt():
    """An Excel compound file named .ppt is read by the xls parser, as sheets."""
    from reader_fix import make_xls
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_xls(path, [{"name": "Data", "cells": {(0, 0): "a", (0, 1): "b"}}])
    out, ctx = render(d, path)
    assert "## Sheet: Data" in out and "|a|b|" in out and not ctx.skipped, (out, ctx.skipped)


def test_powerpoint95_pp40():
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_cfb(path, {"PP40": bytes(4096)})
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("unsupported", "PowerPoint 95 presentation"), got


def test_dual_format_pp40_beside_full_current_user_reads_as_ppt():
    # a 97/95 file keeps PowerPoint 95's PP40 stream beside the 97 streams and a full Current User: it is read as ppt
    d, path, info = build([{"title": "Dual", "body": ["both formats"]}])
    make_cfb(path, {"PowerPoint Document": info["doc_stream"], "Current User": info["current_user"],
                    "Pictures": b"", "PP40": bytes(4096)})
    assert len(info["current_user"]) >= 28
    out, ctx = render(d, path)
    assert "## Slide 1: Dual" in out and "both formats" in out and not ctx.skipped, (out, ctx.skipped)


def test_powerpoint95_current_user():
    """The PowerPoint 95 Current User stream: under 28 bytes, a u32 size and then that many bytes."""
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    make_cfb(path, {"PowerPoint Document": bytes(4096), "Current User": struct.pack("<I", 8) + b"user\0\0\0\0"})
    got = denied(lambda: reader.parse(path, Context()))
    assert got == ("unsupported", "PowerPoint 95 presentation"), got
    make_cfb(path, {"PowerPoint Document": bytes(4096), "Current User": struct.pack("<I", 9) + b"user\0\0\0\0"})
    got = denied(lambda: reader.parse(path, Context()))
    assert got[0] == "damaged", got  # short, but not the PowerPoint 95 form


def test_pptx_saved_as_ppt():
    from test_reader_slides import deck, sp
    src = deck([sp("Renamed title", ph="title")], kind="pptm")
    d = Path(tempfile.mkdtemp())
    path = d / "a.ppt"
    path.write_bytes(Path(src).read_bytes())
    out, _ = render(d, path)
    assert "Renamed title" in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
