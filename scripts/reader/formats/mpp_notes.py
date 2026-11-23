# Follows the text conversion of rtfparserkit (https://github.com/joniles/rtfparserkit), copyright (c) Jon Iles, under
# the Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0), as MPXJ's RtfHelper.strip runs it on notes.
"""The text of a Microsoft Project note, as MPXJ's RtfHelper.strip gives it: rtfparserkit's StringTextConverter
over the RTF, then one last line end dropped.

Text is kept in the main body, `pntext` and `fldrslt` and dropped in every other destination rtfparserkit knows
(an unknown `\\*` destination keeps its text); `\\par`, `\\line` and `\\row` give a line end and `\\tab` and
`\\cell` a tab wherever they stand. Bytes decode with the selected font's charset, else the document code page,
with the Windows code pages (rtfparserkit's Java names map 949, 874 and 936 to the IBM tables, which lose the
extended Hangul and a few marks). Text that is no formal RTF stays as it is, where rtfparserkit drops it; braces
that do not balance, `\\bin` and unknown code pages, where rtfparserkit throws, read as far as they go.
"""
import codecs

TEXT = frozenset({"rtf", "pntext", "fldrslt"})
DESTINATIONS = frozenset("""
aftncn aftnsep aftnsepc annotation atnauthor atndate atnicn atnid atnparent atnref atntime atrfend atrfstart author
background bkmkend bkmkstart blipuid buptim category colorschememapping colortbl comment company creatim datafield
datastore defchp defpap do doccomm docvar dptxbxtext ebcend ebcstart factoidname falt fchars ffdeftext ffentrymcr
ffexitmcr ffformat ffhelptext ffl ffname ffstattext field file filetbl fldinst fldtype fname fontemb fontfile
fonttbl footer footerf footerl footerr footnote formfield ftncn ftnsep ftnsepc g generator gridtbl header headerf
headerl headerr hl hlfr hlinkbase hlloc hlsrc hsv htmltag info keycode keywords latentstyles lchars levelnumbers
leveltext lfolevel linkval list listlevel listname listoverride listoverridetable listpicture liststylename
listtable listtext lsdlockedexcept macc maccPr mailmerge maln malnScr manager margPr mbar mbarPr mbaseJc mbegChr
mborderBox mborderBoxPr mbox mboxPr mchr mcount mctrlPr md mdPr mdeg mdegHide mden mdiff me mendChr meqArr meqArrPr
mf mfName mfPr mfunc mfuncPr mgroupChr mgroupChrPr mgrow mhideBot mhideLeft mhideRight mhideTop mhtmltag mlim
mlimloc mlimlow mlimlowPr mlimupp mlimuppPr mm mmPr mmaddfieldname mmath mmathPict mmathPr mmaxdist mmc mmcJc mmcPr
mmconnectstr mmconnectstrdata mmcs mmdatasource mmheadersource mmmailsubject mmodso mmodsofilter mmodsofldmpdata
mmodsomappedname mmodsoname mmodsorecipdata mmodsosort mmodsosrc mmodsotable mmodsoudl mmodsoudldata mmodsouniquetag
mmquery mmr mnary mnaryPr mnoBreak mnum moMath moMathPara moMathParaPr mobjDist mopEmu mphant mphantPr mplcHide mpos
mr mrPr mrad mradPr msPre msPrePr msSub msSubPr msSubSup msSubSupPr msSup msSupPr msepChr mshow mshp mstrikeBLTR
mstrikeH mstrikeTLBR mstrikeV msub msubHide msup msupHide mtransp mtype mvertJc mvfmf mvfml mvtof mvtol mzeroAsc
mzeroDesc mzeroWid nesttableprops nextfile nonesttables objalias objclass objdata object objname objsect objtime
oldcprops oldpprops oldsprops oldtprops oleclsid operator panose password passwordhash pgp pgptbl picprop pict pn
pnseclvl pntxta pntxtb printim private propname protend protstart protusertbl pxe result revtbl revtim rsidtbl rxe
shp shpgrp shpinst shppict shprslt shptxt sn sp staticval stylesheet subject sv svb tc template themedata title txe
ud userprops wgrffmtfilter windowcaption writereservation writereservhash xe xform xmlattrname xmlattrvalue xmlclose
xmlname xmlnstbl xmlopen
""".split())
BREAKS = {"par": "\n", "line": "\n", "row": "\n", "tab": "\t", "cell": "\t"}
SYMBOLS = {"bullet": "•", "emdash": "—", "endash": "–", "emspace": " ", "enspace": " ",
           "qmspace": " ", "ldblquote": "“", "rdblquote": "”", "lquote": "‘", "rquote": "’"}
DOCUMENT = {"ansi": "cp1252", "mac": "mac_roman", "pc": "cp437", "pca": "cp850"}
FONT_CHARSETS = {0: 1252, 2: 1252, 77: 10000, 78: 10001, 79: 10003, 80: 10008, 81: 10002, 84: 10004, 85: 10006,
                 86: 10081, 88: 10029, 89: 10007, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253,
                 162: 1254, 163: 1258, 177: 1255, 178: 1256, 186: 1257, 204: 1251, 222: 874, 238: 1250, 254: 437,
                 255: 850}
CODE_PAGES = {936: "gbk", 1361: "johab", 65001: "utf-8", 10000: "mac_roman", 10001: "shift_jis", 10002: "big5",
              10003: "euc_kr", 10004: "mac_arabic", 10006: "mac_greek", 10007: "mac_cyrillic", 10008: "gb2312",
              10010: "mac_romanian", 10017: "mac_cyrillic", 10029: "mac_latin2", 10079: "mac_iceland",
              10081: "mac_turkish", 10082: "mac_croatian"}


def codec(page: int):
    """The Python codec of a Windows code page, None for one Python lacks."""
    name = CODE_PAGES.get(page, "cp%d" % page)
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def text(raw) -> str:
    """RtfHelper.strip of a note: "" for none, RTF (after any white space) as its text, other text as it is."""
    if not raw:
        return ""
    if not raw.lstrip().startswith("{\\rtf"):
        return raw
    out = _Converter(raw).run()
    return out[:-1] if out.endswith("\n") and raw.startswith("{\\rtf") else out


class _Group:
    __slots__ = ("dest", "font", "uc", "upr")

    def __init__(self, dest="rtf", font=0, uc=1, upr=False):
        self.dest, self.font, self.uc, self.upr = dest, font, uc, upr

    def copy(self):
        return _Group(self.dest, self.font, self.uc, self.upr)


class _Converter:
    def __init__(self, s: str):
        self.s, self.i = s, 0
        self.out, self.pending, self.bytes = [], [], bytearray()
        self.g, self.stack = _Group(), []
        self.document, self.fonts = "cp1252", {}
        self.skip, self.drop_group = 0, False

    def run(self) -> str:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            self.i += 1
            if c == "{":
                self.event()
                if self.drop_group:
                    self.drop_group = False
                    self.skip_group()
                else:
                    self.stack.append(self.g.copy())
            elif c == "}":
                self.event()
                if self.stack:
                    self.g = self.stack.pop()
            elif c == "\\":
                self.control()
            elif c == "\t":
                self.word("tab", 0)
            elif c not in "\r\n":
                self.char(c)
        if self.stack:
            self.event()
        return "".join(self.out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")

    def char(self, c: str):
        if self.skip:
            self.skip -= 1
        elif ord(c) < 0x100:
            self.bytes.append(ord(c))
        else:
            self.string(c)

    def string(self, t: str):
        self.decode()
        self.pending.append(t)

    def decode(self):
        if self.bytes:
            enc = self.fonts.get(self.g.font) or self.document
            self.pending.append(self.bytes.decode(enc, "replace"))
            self.bytes.clear()

    def event(self):
        self.decode()
        if self.pending:
            if self.g.dest in TEXT:
                self.out.extend(self.pending)
            self.pending.clear()

    def skip_group(self):
        s, depth = self.s, 1
        while self.i < len(s) and depth:
            c = s[self.i]
            self.i += 1
            if c == "\\":
                self.i += 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1

    def control(self):
        s, n = self.s, len(self.s)
        if self.i >= n:
            return
        c = s[self.i]
        self.i += 1
        if "a" <= c <= "z" or "A" <= c <= "Z":
            start = self.i - 1
            while self.i < n and ("a" <= s[self.i] <= "z" or "A" <= s[self.i] <= "Z"):
                self.i += 1
            name, at = s[start:self.i], self.i
            if self.i < n and s[self.i] == "-":
                self.i += 1
            while self.i < n and s[self.i].isascii() and s[self.i].isdigit():
                self.i += 1
            digits = s[at:self.i].lstrip("-")
            param = (-int(digits) if s[at] == "-" else int(digits)) if digits else 0
            if self.i < n and s[self.i] == " ":
                self.i += 1
            self.word(name, param)
        elif c == "'":
            at = self.i
            while self.i < n and self.i - at < 2 and s[self.i] in "0123456789abcdefABCDEF":
                self.i += 1
            if self.i > at:
                if self.skip:
                    self.skip -= 1
                else:
                    self.bytes.append(int(s[at:self.i], 16))
        elif c in "\\{}":
            self.string(c)
        elif c in "\r\n":
            self.word("par", 0)

    def word(self, name: str, param: int):
        self.event()
        g = self.g
        if name == "bin":
            self.i += max(param, 0)
        elif name == "u":
            self.string(chr(param & 0xFFFF))
            self.skip = g.uc
        elif name == "uc":
            g.uc = param
        elif name == "upr":
            g.upr, self.drop_group = True, True
        elif name in BREAKS:
            self.out.append(BREAKS[name])
        elif name in SYMBOLS:
            self.string(SYMBOLS[name])
        elif name in TEXT or name in DESTINATIONS:
            if not (name == "ud" and g.upr):
                g.dest = name
        elif name == "f":
            g.font = param
        elif name in ("fcharset", "cpg"):
            if g.dest == "fonttbl":
                self.fonts[g.font] = codec(FONT_CHARSETS.get(param, -1) if name == "fcharset" else param)
        elif name in DOCUMENT:
            self.document = DOCUMENT[name]
        elif name == "ansicpg":
            self.document = codec(param) or self.document
