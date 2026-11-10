"""A tiny two-bundle project: allowed imports pass, a cross-bundle and an escaping relative import are caught,
the copy test passes for an isolated feature and fails for one that reaches outside, contracts report gaps.

Run: python -I -X utf8 tests/test_modularize.py
"""
import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
import modularize  # noqa: E402

FILES = {
    "shared/msg/__init__.py": "def pack(x):\n    return x + 1\n",
    "app/stt/__init__.py": "from shared.msg import pack\n\ndef run():\n    return pack(1)\n",
    "app/stt/test_stt.py": "from app.stt import run\n\ndef test_run():\n    assert run() == 2\n",
    "control/geo/__init__.py": "def g():\n    return 3\n",
    "control/hazard/__init__.py": "from control.geo import g\nfrom .inner import h\n",
    "control/hazard/inner.py": "def h():\n    return 4\n",
    "control/hazard/test_hazard.py": "from control.hazard import g, h\n\ndef test_it():\n    assert g() + h() == 7\n",
    "control/bad/__init__.py": "from app.stt import run\nfrom ...outside import x\nimport json\n",
    "control/bad/test_bad.py": "import control.bad\n\ndef test_bad():\n    assert control.bad\n",
    "control/hazard/contract/schema/in.json": "{}",
    "control/hazard/contract/examples/one.json": "{}",
    "training/train.py": "from control.hazard import h\n",
    "contracts/README.md": "See control/hazard/contract.\n",
}
RULES = {"features": {"shared/msg": [], "app/stt": ["shared"], "control/geo": [],
                      "control/hazard": ["shared", "control/geo"], "control/bad": ["shared", "control/geo"]},
         "consumers": ["training", "examples"]}


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = modularize.main(list(argv))
    return code, buf.getvalue()


def main():
    root = Path(tempfile.mkdtemp())
    try:
        for rel, text in FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / "contracts/modules.json").write_text(json.dumps(RULES), encoding="utf-8")

        code, out = run("imports", str(root))
        assert code == 1, out
        bad = [l for l in out.splitlines() if l.startswith("FORBIDDEN")]
        assert len(bad) == 2 and all("control/bad" in l for l in bad), out  # cross-bundle + relative escape; json is stdlib
        assert "must not import app/stt" in out and "relative import leaves" in out, out

        if importlib.util.find_spec("pytest") is None:  # copytest runs pytest with this Python: the reason must show
            code, out = run("copytest", str(root), "control/hazard")
            assert code == 1 and "No module named pytest" in out, out
        else:
            code, out = run("copytest", str(root), "control/hazard")
            assert code == 0 and out.startswith("OK"), out
            code, out = run("copytest", str(root), "app/stt")
            assert code == 0, out
            code, out = run("copytest", str(root), "control/bad")
            assert code == 1 and out.startswith("FAIL"), out

        code, out = run("contracts", str(root))
        assert code == 1, out
        assert any(l.startswith("OK\tcontrol/hazard") for l in out.splitlines()), out
        assert "MISSING\tapp/stt" in out and "not linked" in out, out
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("pass")


if __name__ == "__main__":
    main()
