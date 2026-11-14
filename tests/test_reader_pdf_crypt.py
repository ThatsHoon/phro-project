"""pdfnative standard security handler: RC4 and AES (R2-R6) with the empty user password,
crypt filters, unencrypted metadata, refused handlers and the decryption budget.

Run: python -I -X utf8 tests/test_reader_pdf_crypt.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from pdf_fix import EncryptSpec, Name, Ref, Stream, make_pdf, simple_pdf, text_ops  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.pdfnative import crypto  # noqa: E402
from reader.pdfnative.structure import PDFFile  # noqa: E402


def page_text(pf, index=0):
    page, _ = pf.pages()[index]
    return pf.decode_stream_bytes(pf.resolve(page["Contents"]))


def check_text(spec, **kw):
    data = simple_pdf([text_ops("Hi")], encrypt=spec, **kw)
    assert b"(Hi) Tj" not in data  # the content is not in the clear
    pf = PDFFile(data)
    assert b"(Hi) Tj" in page_text(pf)
    return pf


def refused(data, kind, detail=None, **kw):
    try:
        PDFFile(data, **kw)
    except ReadError as e:
        assert e.kind == kind, (e.kind, e.detail)
        if detail is not None:
            assert e.detail == detail, e.detail
        return
    raise AssertionError("expected a ReadError")


def test_r2_rc4():
    check_text(EncryptSpec(2, aes=False))


def test_r3_rc4_128():
    check_text(EncryptSpec(3, aes=False))


def test_r4_aes128():
    check_text(EncryptSpec(4))


def test_r4_rc4_crypt_filter():
    check_text(EncryptSpec(4, aes=False))


def test_r6_aes256():
    check_text(EncryptSpec(6))


def test_strings_decrypted():
    pf = check_text(EncryptSpec(4), catalog={"Lang": "en"})
    assert pf.catalog["Lang"] == b"en"
    for revision in (2, 3, 6):
        pf = PDFFile(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(revision), catalog={"Lang": "en"}))
        assert pf.catalog["Lang"] == b"en", revision


def test_identity_crypt_filter():
    spec = EncryptSpec(4, stream_filter="Identity")
    data = simple_pdf([text_ops("Hi")], encrypt=spec, catalog={"Lang": "en"})
    assert b"(Hi) Tj" in data  # streams are in the clear
    pf = PDFFile(data)
    assert b"(Hi) Tj" in page_text(pf)
    assert pf.catalog["Lang"] == b"en"  # strings still use /StrF


def test_stream_crypt_filter_identity():
    # a stream that names the Identity crypt filter itself is read as it is
    objects = {
        1: {"Type": Name("Catalog"), "Pages": Ref(2)},
        2: {"Type": Name("Pages"), "Kids": [], "Count": 0},
        4: Stream({"Filter": [Name("Crypt")], "DecodeParms": [{"Name": Name("Identity")}]},
                  b"plain"),
    }
    data = make_pdf(objects, encrypt=EncryptSpec(4))
    assert b"plain" in data
    assert PDFFile(data).get_object(Ref(4)).raw == b"plain"


def test_metadata_unencrypted():
    spec = EncryptSpec(4, metadata=False)
    xmp = b"<x:xmpmeta>clear</x:xmpmeta>"
    objects = {
        1: {"Type": Name("Catalog"), "Pages": Ref(2), "Metadata": Ref(4)},
        2: {"Type": Name("Pages"), "Kids": [], "Count": 0},
        4: Stream({"Type": Name("Metadata"), "Subtype": Name("XML")}, xmp),
        5: Stream({}, b"body"),
    }
    data = make_pdf(objects, encrypt=spec)
    assert xmp in data
    pf = PDFFile(data)
    assert pf.get_object(Ref(4)).raw == xmp
    assert pf.decode_stream_bytes(pf.get_object(Ref(5))) == b"body"
    # the other streams stay encrypted
    assert b"body" not in data


def test_user_password():
    refused(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(4, user=b"secret")),
            "encrypted", "user password")
    for revision in (2, 3, 6):
        refused(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(revision, user=b"secret")),
                "encrypted", "user password")


def test_password_given():
    for revision in (2, 4, 6):
        pf = PDFFile(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(revision, user=b"secret")),
                     password="secret")
        assert b"(Hi) Tj" in page_text(pf)


def test_pubsec():
    refused(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(4, filter="Adobe.PubSec")),
            "encrypted", "public-key handler")


def test_other_handler_drm():
    refused(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(4, filter="FOPN_foweb")), "drm")


def test_object_stream_and_xref_stream():
    # strings inside the object stream come out of the stream's decryption only
    for revision in (4, 6):
        data = simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(revision), xref="stream", objstm=True,
                          catalog={"Lang": "en"})
        pf = PDFFile(data)
        assert b"(Hi) Tj" in page_text(pf)
        assert pf.catalog["Lang"] == b"en"
        assert pf.get_object(pf.trailer["Encrypt"]) is not None


def test_survives_junk_retry():
    junk = b"J" * 40
    data = simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(4), junk=junk, shift=len(junk))
    pf = PDFFile(data)
    assert b"(Hi) Tj" in page_text(pf)
    assert pf._handler is not None


def test_plain_pdf_unchanged():
    pf = PDFFile(simple_pdf([text_ops("Hi")]))
    assert pf._handler is None
    assert b"(Hi) Tj" in page_text(pf)


def test_decrypt_budget():
    pf = PDFFile(simple_pdf([text_ops("Hi")], encrypt=EncryptSpec(4)))
    encrypt = pf.resolve(pf.trailer["Encrypt"])
    saved = crypto.DECRYPT_TOTAL
    crypto.DECRYPT_TOTAL = 64
    try:
        handler = crypto.open_handler(encrypt, pf.trailer["ID"][0])
        spec = EncryptSpec(4)
        big = spec.data(9, b"x" * 200, "aesv2")
        try:
            handler.decrypt(9, 0, big)
        except ReadError as e:
            assert e.kind == "too_large", e.kind
        else:
            raise AssertionError("expected too_large")
        small = spec.data(9, b"y" * 20, "aesv2")  # 48 bytes: an IV and two blocks
        assert len(small) == 48
        assert handler.decrypt(9, 0, small) == b"y" * 20  # the refused stream consumed nothing
        assert handler.used == 48
    finally:
        crypto.DECRYPT_TOTAL = saved


def test_budget_cut_counted():
    saved = crypto.DECRYPT_TOTAL
    crypto.DECRYPT_TOTAL = 40
    try:
        pf = PDFFile(simple_pdf([text_ops("Hi" * 50)], encrypt=EncryptSpec(4)))
        assert page_text(pf) == b""
        assert pf.cuts["decrypt_budget"] >= 1
    finally:
        crypto.DECRYPT_TOTAL = saved


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
