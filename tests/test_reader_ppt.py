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


def test_notes():
    out, _ = md([{"title": "Intro", "body": ["text"]}, {"title": "Plan"}], notes={1: "speak slowly"})
    assert "> Notes: speak slowly" in out, out
    assert out.index("> Notes") < out.index("## Slide 2"), out


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
