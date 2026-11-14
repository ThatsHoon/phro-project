"""OMML equations to TeX, and math in Word paragraphs and bodies.

Run: python -I -X utf8 tests/test_reader_omml.py
"""
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader.formats import omml  # noqa: E402
import test_reader_docm as dm  # noqa: E402
import test_reader_slides as sl  # noqa: E402

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def t(body):
    return omml.tex(ET.fromstring(f'<m:oMath xmlns:m="{M}">{body}</m:oMath>'))


def r(x):
    return f"<m:r><m:t>{x}</m:t></m:r>"


def e(x, name="e"):
    return f"<m:{name}>{x}</m:{name}>"


def pr(name, *props):
    return f"<m:{name}>" + "".join(f'<m:{k} m:val="{v}"/>' for k, v in props) + f"</m:{name}>"


def md(body):
    return dm.md(dm.docm(body))


# ---- the walk
def test_anydoc_vector():
    body = (f"<m:f>{e(r('a'), 'num')}{e(r('b'), 'den')}</m:f>"
            f"<m:sSup>{e(r('x'))}{e(r('2'), 'sup')}</m:sSup>"
            f"<m:rad>{pr('radPr', ('degHide', '1'))}<m:deg/>{e(r('y'))}</m:rad>"
            f"<m:nary>{pr('naryPr', ('chr', '∑'), ('limLoc', 'undOvr'))}"
            f"{e(r('i=1'), 'sub')}{e(r('n'), 'sup')}{e(r('i'))}</m:nary>"
            f"<m:d>{pr('dPr', ('begChr', '⟨'), ('endChr', '⟩'))}{e(r('α'))}</m:d>"
            f"<m:func><m:fName><m:r>{pr('rPr', ('sty', 'p'))}<m:t>sin</m:t></m:r></m:fName>{e(r('θ'))}</m:func>")
    assert t(body) == ("\\frac{a}{b}x^{2}\\sqrt{y}\\sum_{i=1}^{n}{i}"
                       "\\left\\langle\\alpha\\right\\rangle\\sin{\\theta}"), t(body)


def test_normal_text_styled():
    body = "<m:r><m:rPr><m:nor/></m:rPr><m:t>for all </m:t></m:r>" + r("𝐱 ∈ ℝ")
    assert t(body) == "\\text{for all }\\mathbf{x} \\in \\mathbb{R}", t(body)


def frac(kind=None):
    head = pr("fPr", ("type", kind)) if kind else ""
    return t(f"<m:f>{head}{e(r('a'), 'num')}{e(r('b'), 'den')}</m:f>")


def test_fraction_types():
    assert frac("lin") == "{a}/{b}", frac("lin")
    assert frac("skw") == "{}^{a}/_{b}", frac("skw")
    assert frac("noBar") == "{a\\atop b}", frac("noBar")
    assert frac() == "\\frac{a}{b}", frac()


def test_scripts():
    assert t(f"<m:sSup>{e(r('x'))}{e(r('2'), 'sup')}</m:sSup>") == "x^{2}"
    assert t(f"<m:sSub>{e(r('x'))}{e(r('i'), 'sub')}</m:sSub>") == "x_{i}"
    assert t(f"<m:sSubSup>{e(r('x'))}{e(r('i'), 'sub')}{e(r('2'), 'sup')}</m:sSubSup>") == "x_{i}^{2}"
    assert t(f"<m:sPre>{e(r('a'), 'sub')}{e(r('b'), 'sup')}{e(r('x'))}</m:sPre>") == "{}_{a}^{b}{x}"


def test_radical():
    assert t(f"<m:rad>{e(r('3'), 'deg')}{e(r('y'))}</m:rad>") == "\\sqrt[3]{y}"
    assert t(f"<m:rad>{pr('radPr', ('degHide', '1'))}{e(r('3'), 'deg')}{e(r('y'))}</m:rad>") == "\\sqrt{y}"


def nary(*props, sub="a", sup="b"):
    return t(f"<m:nary>{pr('naryPr', *props)}{e(r(sub), 'sub') if sub else ''}"
             f"{e(r(sup), 'sup') if sup else ''}{e(r('x'))}</m:nary>")


def test_nary():
    assert nary() == "\\int_{a}^{b}{x}", nary()
    assert nary(("subHide", "1"), ("supHide", "1")) == "\\int{x}"
    assert nary(("chr", "∫"), ("limLoc", "undOvr")) == "\\int\\limits_{a}^{b}{x}"
    assert nary(("chr", "∑"), ("limLoc", "subSup"), sub="i", sup="") == "\\sum\\nolimits_{i}{x}"


def delim(*props, parts=("x",)):
    return t(f"<m:d>{pr('dPr', *props)}" + "".join(e(r(p)) for p in parts) + "</m:d>")


def test_delimiters():
    assert delim() == "\\left(x\\right)"
    assert delim(parts=("a", "b")) == "\\left(a\\middle|b\\right)"
    assert delim(("begChr", "")) == "\\left.x\\right)"
    assert delim(("begChr", "["), ("endChr", "]")) == "\\left[x\\right]"
    assert delim(("sepChr", ""), parts=("a", "b")) == "\\left(a,b\\right)"


def test_matrix_and_eqarr():
    mr = lambda *cells: "<m:mr>" + "".join(e(r(c)) for c in cells) + "</m:mr>"  # noqa: E731
    assert t(f"<m:m>{mr('a', 'b')}{mr('c', 'd')}</m:m>") == "\\begin{matrix} a & b \\\\ c & d \\end{matrix}"
    assert t(f"<m:eqArr>{e(r('a'))}{e(r('b'))}</m:eqArr>") == "\\begin{gathered} a \\\\ b \\end{gathered}"


def test_accents_and_bars():
    assert t(f"<m:acc>{e(r('x'))}</m:acc>") == "\\hat{x}"
    assert t(f"<m:acc>{pr('accPr', ('chr', chr(0x303)))}{e(r('x'))}</m:acc>") == "\\tilde{x}"
    assert t(f"<m:bar>{pr('barPr', ('pos', 'top'))}{e(r('x'))}</m:bar>") == "\\overline{x}"
    assert t(f"<m:bar>{e(r('x'))}</m:bar>") == "\\underline{x}"


def test_group_char():
    assert t(f"<m:groupChr>{e(r('x'))}</m:groupChr>") == "\\underbrace{x}"
    assert t(f"<m:groupChr>{pr('groupChrPr', ('chr', '⏞'), ('pos', 'top'))}{e(r('x'))}</m:groupChr>") == "\\overbrace{x}"
    assert t(f"<m:groupChr>{pr('groupChrPr', ('chr', '='), ('pos', 'top'))}{e(r('x'))}</m:groupChr>") == "\\overset{=}{x}"


def test_limits_and_func():
    assert t(f"<m:limLow>{e(r('x'))}{e(r('n'), 'lim')}</m:limLow>") == "\\underset{n}{x}"
    assert t(f"<m:limUpp>{e(r('x'))}{e(r('n'), 'lim')}</m:limUpp>") == "\\overset{n}{x}"
    lim = f"<m:limLow>{e(r('lim'))}{e(r('n'), 'lim')}</m:limLow>"
    assert t(f"<m:func>{e(lim, 'fName')}{e(r('a'))}</m:func>") == "\\lim_{n}{a}"
    assert t(f"<m:func>{e(r('sinc'), 'fName')}{e(r('x'))}</m:func>") == "\\operatorname{sinc}{x}"


def test_boxes():
    assert t(f"<m:box>{e(r('x'))}</m:box>") == "{x}"
    assert t(f"<m:borderBox>{e(r('x'))}</m:borderBox>") == "\\boxed{x}"
    assert t(f"<m:phant>{pr('phantPr', ('show', '0'))}{e(r('x'))}</m:phant>") == "\\phantom{x}"
    assert t(f"<m:phant>{e(r('x'))}</m:phant>") == "{x}"


def test_unknown_gives_text():
    assert t(f"<m:foo>{r('q')}</m:foo>") == "q"


def test_nesting():
    f = "<m:f>" + e(r("a"), "num") + e(r("b"), "den") + "</m:f>"
    rad = "<m:rad>" + pr("radPr", ("degHide", "1")) + e(f) + "</m:rad>"
    out = t("<m:sSup>" + e(r("x")) + e(rad, "sup") + "</m:sSup>")
    assert out == "x^{\\sqrt{\\frac{a}{b}}}", out


def test_deep_nesting_keeps_text():
    n = 5_000
    body = "<m:sSup><m:e>" * n + r("z") + ("</m:e><m:sup>" + r("1") + "</m:sup></m:sSup>") * n
    el = ET.fromstring(f'<m:oMath xmlns:m="{M}">{body}</m:oMath>')
    start = time.perf_counter()
    out = omml.tex(el)
    assert time.perf_counter() - start < 1
    assert "z" in out and out.count("1") == n, out[:200]


# ---- Word
def test_word_inline_math():
    sup = f"<m:sSup>{e(r('a'))}{e(r('2'), 'sup')}</m:sSup>"
    out = md(f'<w:p><w:r><w:t>Area </w:t></w:r><m:oMath xmlns:m="{M}">{sup}</m:oMath></w:p>')
    assert "Area $a^{2}$" in out, out


def test_word_display_math():
    out = md(f'<w:p><m:oMathPara xmlns:m="{M}"><m:oMath>{r("x")}</m:oMath><m:oMath>{r("y")}</m:oMath>'
             '</m:oMathPara></w:p>')
    ls = out.splitlines()
    assert "$$x$$" in ls and "$$y$$" in ls and ls.index("$$x$$") < ls.index("$$y$$"), out
    assert "$x$" not in ls, out


def test_word_body_level_math():
    out = md(f'<m:oMathPara xmlns:m="{M}"><m:oMath>{r("x")}</m:oMath></m:oMathPara>')
    assert "$$x$$" in out.splitlines(), out
    out = md(f'<m:oMath xmlns:m="{M}">{r("x")}</m:oMath>')
    assert "$x$" in out.splitlines(), out


def test_word_deleted_display_math_left_out():
    out = md(f'<w:p><w:del><m:oMathPara xmlns:m="{M}"><m:oMath>{r("q")}</m:oMath></m:oMathPara></w:del></w:p>')
    assert "q" not in out, out


def test_math_not_flat_text():
    sup = f"<m:sSup>{e(r('a'))}{e(r('2'), 'sup')}</m:sSup>"
    out = md(f'<w:p><w:r><w:t>Area </w:t></w:r><m:oMath xmlns:m="{M}">{sup}</m:oMath></w:p>')
    assert "a2" not in out, out


A14 = "http://schemas.microsoft.com/office/drawing/2010/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PNS = f'xmlns:m="{M}" xmlns:a14="{A14}"'


def sup(base, exp):
    return f"<m:sSup><m:e>{r(base)}</m:e><m:sup>{r(exp)}</m:sup></m:sSup>"


def test_slide_math_replaces_fallback():
    p = f'<a:p {PNS}><a14:m><m:oMathPara><m:oMath>{sup("x", "2")}</m:oMath></m:oMathPara></a14:m></a:p>'
    pic = ('<p:pic><p:nvPicPr><p:cNvPr id="9" name="p"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
           '<p:blipFill><a:blip r:embed="rI"/></p:blipFill><p:spPr/></p:pic>')
    alt = (f'<mc:AlternateContent xmlns:mc="{MC}" xmlns:a14="{A14}"><mc:Choice Requires="a14">{sl.sp(paras=p)}'
           f'</mc:Choice><mc:Fallback>{pic}</mc:Fallback></mc:AlternateContent>')
    path = sl.deck([alt], srels={1: [("rI", "image", "../media/i.png")]}, extra={"ppt/media/i.png": sl.PNG})
    out = sl.md(path)
    assert "$$x^{2}$$" in out and "assets/" not in out, out


def test_slide_inline_math():
    p = f'<a:p {PNS}><a:r><a:t>E = </a:t></a:r><a14:m><m:oMath><m:sSup><m:e>{r("m")}{r("c")}</m:e><m:sup>{r("2")}</m:sup></m:sSup></m:oMath></a14:m></a:p>'
    out = sl.md(sl.deck([sl.sp(paras=p)]))
    assert "E = ${mc}^{2}$" in out, out  # omml braces a multi-letter base


def test_slide_math_in_table_cell():
    cell = (f'<a:tc><a:txBody><a:bodyPr/><a:p {PNS}><a14:m><m:oMathPara><m:oMath>{r("x")}'
            '</m:oMath></m:oMathPara></a14:m></a:p></a:txBody></a:tc>')
    frame = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="t"/><p:cNvGraphicFramePr/><p:nvPr/>'
             '</p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl><a:tblGrid><a:gridCol/></a:tblGrid>'
             f'<a:tr>{cell}</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    out = sl.md(sl.deck([frame]))
    assert "$x$" in out and "$$" not in out, out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
