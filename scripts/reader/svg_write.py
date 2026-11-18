"""A vector.Drawing as SVG 1.1, for resvg_py to draw where GDI+ is not there (macOS and Linux).

Paths come out with stroke, fill and fill-rule; text as <text> with the run's face (plus a generic fallback and, for
CJK text, a CJK face), size, weight, style, color and rotation; bitmaps as data: URIs; clips as clipPath. Every text
and attribute is XML-escaped, characters XML 1.0 forbids are dropped, and every number is finite and within +-2^31.
The SVG holds no external reference, no script and no DOCTYPE, so the renderer has nothing to fetch.
"""
import base64
import math
import re

from .vector import Bitmap, Clip, Path, Run

_LIMIT = 2.0 ** 31
_BAD_XML = re.compile("[^\t\n\r -퟿-�\U00010000-\U0010ffff]")
_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"}

_KR = "Malgun Gothic, Apple SD Gothic Neo, Noto Sans CJK KR, sans-serif"
_JP = "Yu Gothic, Hiragino Sans, Noto Sans CJK JP"
_SC = "Microsoft YaHei, PingFang SC, Noto Sans CJK SC"
_TC = "Microsoft JhengHei, PingFang TC, Noto Sans CJK TC"
_SERIF = re.compile("Times|Roman|Serif|Batang|Myungjo|Mincho", re.I)
_KOREAN_FACE = re.compile("Malgun|Batang|Gulim|Dotum|Gungsuh|Myungjo|Nanum|Apple SD", re.I)
_TC_FACE = re.compile("JhengHei|MingLiU|PMingLiU|PingFang TC|DFKai|Noto Sans CJK TC|Noto Serif CJK TC", re.I)
_SC_FACE = re.compile("YaHei|SimSun|SimHei|FangSong|KaiTi|PingFang SC|STSong|STHeiti|Noto Sans CJK SC|"
                      "Noto Serif CJK SC", re.I)
_JP_FACE = re.compile("Yu Gothic|Yu Mincho|MS Gothic|MS Mincho|MS PGothic|MS PMincho|Meiryo|Hiragino|"
                      "Noto Sans CJK JP|Noto Serif CJK JP", re.I)
_HANGUL = re.compile("[ᄀ-ᇿ㄰-㆏가-힯]")
_KANA = re.compile("[぀-ヿㇰ-ㇿｦ-ﾟ]")
_HAN = re.compile("[㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f]")


def _x(s) -> str:
    """s as XML text or attribute content: forbidden characters dropped, the five markup characters escaped."""
    return "".join(_ESC.get(c, c) for c in _BAD_XML.sub("", str(s)))


def _n(v) -> str:
    """A number for SVG: finite, within +-2^31, no exponent, no trailing zeros."""
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return "0"
    if not math.isfinite(v):
        return "0"
    v = max(-_LIMIT, min(_LIMIT - 1, v))
    if v == int(v):
        return str(int(v))
    r = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if r in ("", "-0") else r


def _f(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _rgb(c) -> str:
    return "#%06x" % ((int(c) if isinstance(c, int) else 0) & 0xFFFFFF)


def _family(face, text: str) -> str:
    """The font-family list: the face, then the CJK faces of the text's script (the face's own script first), then
    a generic family."""
    face = str(face or "")
    if _KANA.search(text):
        script = "jp"
    elif _HANGUL.search(text):
        script = "kr"
    elif _HAN.search(text):
        script = ("kr" if _KOREAN_FACE.search(face) else "tc" if _TC_FACE.search(face) else
                  "sc" if _SC_FACE.search(face) else "jp" if _JP_FACE.search(face) else "kr")
    else:
        script = ""
    if script:
        own = {"jp": _JP, "sc": _SC, "tc": _TC}.get(script)
        tail = _KR if own is None else own + ", " + _KR
    elif _SERIF.search(face) and not re.search("sans", face, re.I):
        tail = "serif"
    else:
        tail = "sans-serif"
    return f"{face}, {tail}" if face.strip() else tail


def _d(segs) -> str:
    out = []
    for s in segs or ():
        if not isinstance(s, (tuple, list)) or not s:
            continue
        op = s[0]
        if op == "M" and len(s) >= 3:
            out.append(f"M{_n(s[1])} {_n(s[2])}")
        elif op == "L" and len(s) >= 3:
            out.append(f"L{_n(s[1])} {_n(s[2])}")
        elif op == "C" and len(s) >= 7:
            out.append("C" + " ".join(_n(v) for v in s[1:7]))
        elif op == "Z":
            out.append("Z")
    return " ".join(out)


def write(d, size, resolve=None) -> str:
    """The SVG of drawing `d`, `size` pixels wide and high (a viewBox equal to the frame, a white rect first).
    `resolve(run)` turns a glyph-index run into its text, as for vector.text_lines."""
    x0, y0, x1, y1 = (_f(v) for v in d.frame)
    vx, vy = min(x0, x1), min(y0, y1)
    vw, vh = abs(x1 - x0), abs(y1 - y0)
    if vw <= 0:
        vw = 1.0
    if vh <= 0:
        vh = 1.0
    w = max(1, int(_f(size[0]))) if size else 1
    h = max(1, int(_f(size[1]))) if size else 1
    hair = vw / w  # frame units to a device pixel: the width of a pen of width 0
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="%s %s %s %s">'
           % (w, h, _n(vx), _n(vy), _n(vw), _n(vh)),
           '<rect x="%s" y="%s" width="%s" height="%s" fill="#ffffff"/>' % (_n(vx), _n(vy), _n(vw), _n(vh))]
    nclip = 0
    group = False
    for it in d.items:
        if isinstance(it, Clip):
            if group:
                out.append("</g>")
                group = False
            if it.segs is not None:
                nclip += 1
                path = _d(it.segs) or "M0 0 L0 0 Z"  # an empty clip shows nothing
                out.append(f'<clipPath id="c{nclip}"><path d="{path}"/></clipPath><g clip-path="url(#c{nclip})">')
                group = True
        elif isinstance(it, Path):
            path = _d(it.segs)
            if not path or (it.pen is None and it.brush is None):
                continue
            a = [f'd="{path}"']
            if it.pen is not None:
                a.append(f'stroke="{_rgb(it.pen.color)}" stroke-width="{_n(max(_f(it.pen.width), hair))}"')
            else:
                a.append('stroke="none"')
            a.append(f'fill="{_rgb(it.brush.color)}"' if it.brush is not None else 'fill="none"')
            a.append(f'fill-rule="{"evenodd" if it.even_odd else "nonzero"}"')
            out.append("<path " + " ".join(a) + "/>")
        elif isinstance(it, Run):
            text = it.text
            if not text and it.glyphs is not None and resolve is not None:
                try:
                    text = resolve(it)
                except Exception:  # noqa: BLE001 - an unresolvable run is left out
                    text = None
            if not text or not str(text).strip():
                continue
            text = _BAD_XML.sub("", str(text))
            if not text.strip():
                continue
            ang = _f(it.angle)
            rot = f' transform="rotate({_n(-ang)} {_n(it.x)} {_n(it.y)})"' if ang % 360 != 0 else ""
            weight = it.weight if isinstance(it.weight, int) and 100 <= it.weight <= 900 else 400
            out.append(
                f'<text x="{_n(it.x)}" y="{_n(it.y)}" font-family="{_x(_family(_BAD_XML.sub("", str(it.face or "")), text))}" '
                f'font-size="{_n(abs(_f(it.height)))}" font-weight="{weight}" '
                f'font-style="{"italic" if it.italic else "normal"}" fill="{_rgb(it.color)}"{rot} '
                f'xml:space="preserve">{_x(text)}</text>')
        elif isinstance(it, Bitmap):
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(it.kind)
            if mime is None or not isinstance(it.data, (bytes, bytearray)):
                continue
            bx0, by0, bx1, by1 = (_f(v) for v in it.box)
            out.append(f'<image x="{_n(min(bx0, bx1))}" y="{_n(min(by0, by1))}" width="{_n(abs(bx1 - bx0))}" '
                       f'height="{_n(abs(by1 - by0))}" preserveAspectRatio="none" '
                       f'href="data:{mime};base64,{base64.b64encode(bytes(it.data)).decode("ascii")}"/>')
    if group:
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)
