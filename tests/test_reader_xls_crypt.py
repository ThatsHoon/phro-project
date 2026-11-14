"""xls default-password decryption: XOR, RC4 and CryptoAPI with VelvetSweatshop; any other password stays encrypted.

Run: python -I -X utf8 tests/test_reader_xls_crypt.py
"""
import struct
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import reader  # noqa: E402
from reader import limits  # noqa: E402
from reader.formats import xls_crypt  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader_fix import biff as rec, encrypt_xls, make_cfb, rc4_header  # noqa: E402

SHEETS = [{"name": "First", "cells": {(0, 0): "alpha", (0, 1): 42.5, (1, 0): "한글 text", (2, 2): 7}},
          {"name": "Second", "cells": {(0, 0): "beta " * 30, (1, 1): "gamma"}}]
TEXTS = ("alpha", "42.5", "한글 text", "gamma", "## Sheet: Second")


def convert(stream):
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.xls"
        make_cfb(src, {"Workbook": stream})
        reader.convert(src, Path(d) / "out")
        return (Path(d) / "out" / "out.md").read_text(encoding="utf-8")


def denied(fn):
    try:
        fn()
    except ReadError as e:
        return e.kind, e.detail
    raise AssertionError("expected a ReadError")


def check(kind, **kw):
    md = convert(encrypt_xls(SHEETS, "VelvetSweatshop", kind, **kw))
    for t in TEXTS:
        assert t in md, (kind, t, md)


def test_xor_velvet():
    check("xor")
    check("xor", sst_split=24)  # CONTINUE bodies are records of their own


def test_rc4_velvet():
    check("rc4")
    check("rc4", sst_split=24)


def test_cryptoapi_velvet():
    check("cryptoapi")
    check("cryptoapi40")


def test_truncated_encrypted_like_clear():
    """A stream cut inside its last record reads as the clear workbook cut the same way: the first sheet, and the
    short record counted, never the whole file denied."""
    from reader.formats import sheet_grid, xls
    from reader.model import Context
    from reader_fix import biff_workbook
    sheets = [{"name": "A", "cells": {(0, 0): "alpha", (1, 0): 1.5}},
              {"name": "B", "cells": {(0, 0): "beta", (1, 0): 2.5, (2, 0): "gamma"}}]

    def read(data):
        ctx = Context()
        secs = xls.Workbook(data[:-7], ctx, sheet_grid.Budget()).sections()
        return [(s.title, [str(b) for b in s.blocks]) for s in secs], dict(ctx.skipped)

    clear = read(biff_workbook(sheets))
    assert clear[0][0][0] == "A" and clear[1] == {"record": 1}, clear
    for kind in ("cryptoapi", "xor"):
        got = read(encrypt_xls(sheets, "VelvetSweatshop", kind))
        assert got == clear, (kind, got, clear)


def test_user_password():
    for kind in ("xor", "rc4", "cryptoapi", "cryptoapi40"):
        got = denied(lambda: convert(encrypt_xls(SHEETS, "secret", kind)))
        assert got[0] == "encrypted", (kind, got)


def test_xor_known_answer():
    # computed once with dochan crypto/legacy.py (_xor_array_bytes accepts exactly these for "VelvetSweatshop")
    assert xls_crypt.xor_key("VelvetSweatshop") == 0xB359
    assert xls_crypt.xor_verifier("VelvetSweatshop") == 0x9A0A
    assert xls_crypt.xor_array("VelvetSweatshop", 0xB359).hex() == "876b9ae21ee305621e699660986e9404"


def filepass_stream(body):
    head = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0, 0, 0, 6))
    return head + rec(0x002F, body), (0x002F, len(head) + 4, len(body), ())


def test_unknown_type():
    data, fp = filepass_stream(struct.pack("<H", 2) + bytes(20))
    assert denied(lambda: xls_crypt.decrypt(data, fp))[0] == "encrypted", "type 2"
    data, fp = filepass_stream(struct.pack("<H", 1) + b"\x09\x00\x09\x00" + bytes(60))  # unknown RC4 header version
    assert denied(lambda: xls_crypt.decrypt(data, fp))[0] == "encrypted"
    data, fp = filepass_stream(struct.pack("<HHH", 0, 1, 2) + b"x")  # XOR body of the wrong size
    assert denied(lambda: xls_crypt.decrypt(data, fp))[0] == "encrypted"


def test_decrypt_cap():
    stream = encrypt_xls(SHEETS, "VelvetSweatshop", "rc4")
    with mock.patch.object(limits, "DECRYPT_BYTES", 1000):
        got = denied(lambda: convert(stream))
    assert got == ("too_large", "part: Workbook"), got


def big_stream(header):
    head = rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0, 0, 0, 6))
    fp = rec(0x002F, header)
    body = b"".join(rec(0x1234, bytes(range(256)) * 32) for _ in range(512))  # 4 MiB of 8 KiB records
    return head + fp + body, (0x002F, len(head) + 4, len(header), ())


def test_throughput():
    for header in (struct.pack("<H", 1) + rc4_header("VelvetSweatshop", cryptoapi=False),
                   struct.pack("<HHH", 0, xls_crypt.xor_key("VelvetSweatshop"),
                               xls_crypt.xor_verifier("VelvetSweatshop"))):
        data, fp = big_stream(header)
        t = time.perf_counter()
        out = xls_crypt.decrypt(data, fp)
        took = time.perf_counter() - t
        assert out is not None and len(out) == len(data)
        assert took < 3, (header[:2], took)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
