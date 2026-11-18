import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import read


def test_internal_error_keeps_document_text_out():
    def boom(src):
        raise RuntimeError("line 3 of the document: account 1234")

    read._reexec, read.reader.precheck = (lambda a: None), boom
    args = SimpleNamespace(file="x.docx", engine=None, pages=None, out=None, key=None, hidden=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = read.cmd_convert(args)
    assert code == 1 and out.getvalue() == "FAIL\tdamaged\tinternal error RuntimeError\n", out.getvalue()


if __name__ == "__main__":
    test_internal_error_keeps_document_text_out()
    print("ok")
