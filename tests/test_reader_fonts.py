"""Fonts: finding a face in the system font folders and turning glyph numbers back into text.

Every test points PHRO_READER_FONTS and PHRO_READER_CACHE at temporary folders, so no real font is indexed and the
real cache is never written.

Run: python -I -X utf8 tests/test_reader_fonts.py
"""
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from metafile_fix import ttc, ttf  # noqa: E402
from reader import cache, fonts, limits  # noqa: E402
from reader.vector import Run  # noqa: E402


class Env:
    """A temporary font folder and cache root, with the per-process memos cleared on entry and exit."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "fonts"
        self.dir.mkdir()
        self.old = {k: os.environ.get(k) for k in ("PHRO_READER_FONTS", "PHRO_READER_CACHE")}
        os.environ["PHRO_READER_FONTS"] = str(self.dir)
        os.environ["PHRO_READER_CACHE"] = str(Path(self.tmp.name) / "cache")
        self.caps = (limits.FONT_FILES, limits.FONT_BYTES)
        fonts.reset()
        return self

    def put(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def __exit__(self, *exc):
        limits.FONT_FILES, limits.FONT_BYTES = self.caps
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        fonts.reset()
        fonts.UNMAPPED = False
        self.tmp.cleanup()


def run(face, glyphs):
    return Run("", 0, 0, face, 10, glyphs=tuple(glyphs))


def test_find_by_family_case_insensitive():
    with Env() as e:
        p = e.put("a.ttf", ttf("Sample Sans", {65: 1}))
        assert fonts.find("sample SANS") == p
        assert fonts.find("Other") is None


def test_find_by_full_name():
    with Env() as e:
        p = e.put("a.ttf", ttf("Sample", {65: 1}, full="Sample Bold Italic"))
        assert fonts.find("sample bold italic") == p


def test_name_ids_16_and_mac_roman():
    with Env() as e:
        p = e.put("a.ttf", ttf("Sample", {65: 1}, extra_names=[(3, 1, 16, "Typographic Family"), (1, 0, 1, "Café Mac")]))
        assert fonts.find("typographic family") == p
        assert fonts.find("café mac") == p


def test_full_name_beats_typographic_family():
    with Env() as e:
        # a narrow cut lists "Plain" as its typographic family (ID 16) and sorts first; the real "Plain" wins
        e.put("a_narrow.ttf", ttf("Plain Narrow", {65: 1}, extra_names=[(3, 1, 16, "Plain")]))
        p = e.put("b_plain.ttf", ttf("Plain", {65: 1}))
        assert fonts.find("plain") == p
        assert fonts.find("plain narrow") == e.dir / "a_narrow.ttf"


def test_ttc_second_face():
    with Env() as e:
        p = e.put("c.ttc", ttc([("First", {65: 1}), ("Second", {66: 1, 67: 2})]))
        assert fonts.locate("Second") == (p, 1)
        assert fonts.locate("First") == (p, 0)
        assert fonts.find("Second") == p
        assert fonts.glyph_map(p, 1) == {1: 66, 2: 67}
        assert fonts.glyph_map(p, 0) == {1: 65}
        assert fonts.resolve(run("Second", [2, 1])) == "CB"


def test_glyph_map_lowest_codepoint():
    with Env() as e:
        p = e.put("a.ttf", ttf("S", {0x62: 5, 0x41: 5, 0x43: 6}))
        assert fonts.glyph_map(p) == {5: 0x41, 6: 0x43}


def test_glyph_map_idrangeoffset_and_notdef():
    with Env() as e:
        p = e.put("a.ttf", ttf("S", {0x41: 3, 0x42: 0, 0x43: 4}, ranged=True))
        assert fonts.glyph_map(p) == {3: 0x41, 4: 0x43}  # glyph 0 never maps


def test_format12_astral():
    with Env() as e:
        p = e.put("a.ttf", ttf("S", {0x41: 1, 0x1F600: 2, 0x10000: 3}))
        assert fonts.glyph_map(p) == {1: 0x41, 2: 0x1F600, 3: 0x10000}
        assert fonts.resolve(run("S", [2, 1])) == "\U0001F600A"


def test_resolve_drops_unmapped_and_flags():
    with Env() as e:
        e.put("a.ttf", ttf("S", {0x48: 1, 0x69: 2}))
        fonts.UNMAPPED = False
        assert fonts.resolve(run("S", [1, 99, 2])) == "Hi"
        assert fonts.UNMAPPED is True
        fonts.UNMAPPED = False
        assert fonts.resolve(run("S", [1, 2])) == "Hi"
        assert fonts.UNMAPPED is False


def test_resolve_no_font_none():
    with Env():
        fonts.UNMAPPED = False
        assert fonts.resolve(run("Nobody", [1, 2])) is None
        assert fonts.UNMAPPED is True


def test_index_rebuilt_on_mtime_change():
    with Env() as e:
        p = e.put("a.ttf", ttf("Before", {65: 1}))
        assert fonts.find("Before") == p
        rows = json.loads((cache.root() / "fonts.json").read_text(encoding="utf-8"))
        assert rows[0][0] == str(p) and rows[0][1] == p.stat().st_size and rows[0][3] == [["before"]]
        p.write_bytes(ttf("After", {65: 1}))
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        fonts.reset()
        assert fonts.find("After") == p
        assert fonts.find("Before") is None


def test_index_reused_when_unchanged():
    with Env() as e:
        p = e.put("a.ttf", ttf("Kept", {65: 1}))
        fonts.find("Kept")
        idx = cache.root() / "fonts.json"
        rows = json.loads(idx.read_text(encoding="utf-8"))
        rows[0][3] = [["planted"]]  # a row whose size and mtime still match is not parsed again
        idx.write_text(json.dumps(rows), encoding="utf-8")
        fonts.reset()
        assert fonts.find("planted") == p
        assert fonts.find("Kept") is None


def test_font_files_cap():
    with Env() as e:
        e.put("a.ttf", ttf("Alpha", {65: 1}))
        e.put("b.ttf", ttf("Beta", {65: 1}))
        limits.FONT_FILES = 1
        assert fonts.find("Alpha") is not None
        assert fonts.find("Beta") is None
        rows = json.loads((cache.root() / "fonts.json").read_text(encoding="utf-8"))
        assert len(rows) == 1


def test_font_bytes_cap():
    with Env() as e:
        data = ttf("Big", {65: 1}) + b"\0" * 4096
        p = e.put("a.ttf", data)
        limits.FONT_BYTES = len(data) - 1
        assert fonts.find("Big") is None
        assert fonts.glyph_map(p) == {}
        limits.FONT_BYTES = len(data)
        fonts.reset()
        assert fonts.find("Big") == p


def test_damaged_font_skipped():
    with Env() as e:
        good = ttf("Good", {65: 1})
        e.put("a_trunc.ttf", good[:40])
        e.put("b_junk.ttf", b"\xff" * 300)
        e.put("c_empty.ttf", b"")
        e.put("d_ttc.ttc", b"ttcf" + struct.pack(">II", 0x10000, 0xFFFFFFFF) + b"\0" * 20)
        bad = bytearray(good)
        bad[12 + 16 + 8:12 + 16 + 12] = struct.pack(">I", 0xFFFFFF00)  # the name table's offset points past the end
        e.put("e_badname.ttf", bytes(bad))
        hostile = bytearray(good)
        hostile[12 + 8:12 + 12] = struct.pack(">I", 0x7FFFFFF0)  # the cmap table's offset
        q = e.put("zz_badcmap.ttf", bytes(hostile))
        p = e.put("z_good.ttf", good)
        assert fonts.find("Good") == p
        assert fonts.glyph_map(q) == {}
        assert fonts.glyph_map(e.dir / "a_trunc.ttf") == {}
        assert fonts.glyph_map(e.dir / "missing.ttf") == {}


def test_damaged_cmap_segments():
    with Env() as e:
        good = bytearray(ttf("S", {0x41: 1}))
        p = e.put("a.ttf", bytes(good))
        off = struct.unpack(">I", bytes(good)[12 + 8:12 + 12])[0]  # cmap table offset
        sub = off + 4 + 8  # past the cmap header and its one record: the format 4 subtable
        good[sub + 6:sub + 8] = struct.pack(">H", 0xFFFE)  # segCountX2 far beyond the table
        q = e.put("b.ttf", bytes(good))
        assert fonts.glyph_map(p) == {1: 0x41}
        assert fonts.glyph_map(q) == {}


def test_resolve_never_raises():
    with Env() as e:
        e.put("a.ttf", ttf("S", {65: 1}))
        fonts.UNMAPPED = False
        assert fonts.resolve(Run("", 0, 0, None, 10, glyphs=(1,))) is None
        assert fonts.UNMAPPED is True
        fonts.UNMAPPED = False
        assert fonts.resolve(Run("", 0, 0, "S", 10, glyphs=("x", None))) is None
        assert fonts.UNMAPPED is True


def test_font_dirs_env_and_default():
    with Env() as e:
        other = e.dir.parent / "other"
        os.environ["PHRO_READER_FONTS"] = os.pathsep.join([str(e.dir), str(other)])
        assert fonts.font_dirs() == [e.dir, other]
        del os.environ["PHRO_READER_FONTS"]
        assert fonts.font_dirs()  # the OS list


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
