"""A small real project moved end to end: its own test passes before and after, verify catches a planted break, undo restores every byte.

Run: python -I -X utf8 tests/test_restructure.py
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SCRIPT = HERE / "scripts/restructure.py"
TEMPLATE = HERE / "skills/restructure/references/test_structure.py"
sys.path.insert(0, str(SCRIPT.parent))
import restructure  # noqa: E402

FILES = {
    "run.py": 'from lib.util import f\nfrom lib import helper\nimport json\nif __name__ == "__main__":\n    print(f(), helper.g(), json.load(open("data/table.json"))["a"])\n',
    "lib/__init__.py": "",
    "lib/util.py": "from .helper import g\n\n\ndef f():\n    return g() + 1\n",
    "lib/helper.py": "def g():\n    return 41\n",
    "lib/frag.py": "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parents[1]))\n",
    "lib/alias.py": "import lib.helper as h\nimport lib.util\n",
    "tests/__init__.py": "",
    "tests/test_run.py": 'from lib.util import f\n\nassert f() == 42\nprint("pass")\n',
    "docs/guide.md": "See [run](../run.py), `lib/util.py`, [table](../data/table.json) and `C:/nowhere/x.py`.\n",
    "docs/tool.py": "print('code in docs')\n",
    "web/index.html": '<script src="js/main.js"></script>\n',
    "web/js/main.js": "const dep = require('./dep');\nconst table = require('../../data/table.json');\nmodule.exports = dep + table.a;\n",
    "web/js/dep.js": "module.exports = 1;\n",
    "data/table.json": '{"a": 1}\n',
    "backup_old/copy_of_helper.py": "def g():\n    return 41\n",
    "scripts/job.sh": 'python "$PROJECT/run.py"\n',
    "unused.py": "VALUE = 1\n",
    "web/notes.md": "See `../web` for the page.\n",
    "sub/main.py": "from routers.a import b\n",
    "sub/routers/a.py": "b = 1\n",
    "sub/tools/job.py": "from routers.a import b\n",
}
PAD = "# padding so the duplicate check, which ignores tiny files, sees this file.\n" * 4
FILES["lib/helper.py"] += PAD
FILES["backup_old/copy_of_helper.py"] += PAD
MOVES = {"lib": "core/shared", "web/js": "app/js", "backup_old": "legacy/backup_old", "docs/tool.py": "tools/tool.py", "data/table.json": "assets/table.json", "sub": "legacy/sub"}


def build(root):
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def put(root, rel, text):
    (root / rel).write_bytes(text.encode("utf-8"))


def tool(root, *args):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8", cwd=root)


def own_test(root):
    return subprocess.run([sys.executable, "-m", "tests.test_run"], capture_output=True, text=True, cwd=root)


def fingerprint(root):
    skip = root / ".phro-project"
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
            if p.is_file() and skip not in p.parents and "__pycache__" not in p.parts}


def plan_file(folder, moves):
    path = folder / "moves.json"
    path.write_text(json.dumps({"moves": moves}), encoding="utf-8")
    return str(path)


def edges(scan, src):
    return {(e["dst"], e["how"]) for e in scan.edges if e["src"] == src}


def demo():
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        root = folder / "proj"
        root.mkdir()
        build(root)
        assert own_test(root).stdout.strip() == "pass"

        scan = restructure.Scan(root)
        assert ("lib/util.py", "import") in edges(scan, "run.py") and ("lib/helper.py", "import") in edges(scan, "run.py")
        assert ("lib/helper.py", "import") in edges(scan, "lib/util.py")
        assert ("run.py", "path") in edges(scan, "docs/guide.md") and ("lib/util.py", "path") in edges(scan, "docs/guide.md")
        assert ("web/js/main.js", "path") in edges(scan, "web/index.html")
        assert ("web/js/dep.js", "js") in edges(scan, "web/js/main.js") and ("data/table.json", "path") in edges(scan, "web/js/main.js")
        assert ("run.py", "path") in edges(scan, "scripts/job.sh"), "a path after an environment variable counts"
        assert not any(e["dst"] == "x.py" or "nowhere" in e["token"] for e in scan.edges), "a path outside the project does not resolve"
        found = restructure.smells(scan)
        assert "docs/tool.py" in found["code-in-docs"] and "backup_old" in found["backup-folder"] and "unused.py" in found["orphan-code"]
        assert any("copy_of_helper" in row for row in found["duplicate"])
        assert {"sys-path", "parents", "file-dir"} <= {row[1] for row in scan.fragile}

        original = fingerprint(root)
        plan = plan_file(folder, MOVES)
        out = tool(root, "plan", str(root), plan)
        assert out.returncode == 0 and "MANUAL\t1" in out.stdout and "dotted import" in out.stdout, out.stdout + out.stderr
        assert fingerprint(root) == original, "plan writes nothing"

        for bad in ({"gone": "x"}, {"lib": "run.py"}, {"lib": "lib/inner"}, {"lib": "../out"}, {"lib": "same", "web": "SAME"}):
            refused = tool(root, "apply", str(root), plan_file(folder, bad))
            assert refused.returncode == 1 and "error" in refused.stdout, (bad, refused.stdout, refused.stderr)
        assert fingerprint(root) == original, "a refused plan changes nothing"

        out = tool(root, "apply", str(root), plan_file(folder, MOVES))
        assert out.returncode == 0, out.stdout + out.stderr
        stamp = out.stdout.split("\n")[0].split("\t")[1]
        text = lambda rel: (root / rel).read_text(encoding="utf-8")
        assert "from core.shared.util import f" in text("run.py") and "from core.shared import helper" in text("run.py")
        assert '"assets/table.json"' in text("run.py")
        assert "from .helper import g" in text("core/shared/util.py"), "a relative import between files that moved together stays"
        assert "import core.shared.helper as h" in text("core/shared/alias.py")
        assert "import lib.util" in text("core/shared/alias.py"), "a dotted import without alias waits for a person"
        assert "(../run.py)" in text("docs/guide.md") and "`core/shared/util.py`" in text("docs/guide.md") and "(../assets/table.json)" in text("docs/guide.md")
        assert 'src="../app/js/main.js"' in text("web/index.html")
        assert "require('./dep')" in text("app/js/main.js") and "require('../../assets/table.json')" in text("app/js/main.js")
        assert "$PROJECT/run.py" in text("scripts/job.sh")
        assert text("web/notes.md") == FILES["web/notes.md"], "a reference that still points the same way keeps its spelling"
        assert text("legacy/sub/main.py") == FILES["sub/main.py"] and text("legacy/sub/tools/job.py") == FILES["sub/tools/job.py"], "a whole subproject keeps its own imports"
        assert not (root / "lib").exists() and not (root / "web/js").exists(), "emptied folders go"

        assert own_test(root).stdout.strip() == "pass", "the project's own test still passes"
        assert tool(root, "verify", str(root), stamp).returncode == 1, "the dotted import is still broken"
        put(root, "core/shared/alias.py", "import core.shared.helper as h\nimport core.shared.util\n")
        ok = tool(root, "verify", str(root), stamp)
        assert ok.returncode == 0 and "VERIFIED" in ok.stdout, ok.stdout + ok.stderr

        saved = text("core/shared/util.py")
        put(root, "core/shared/util.py", saved.replace("from .helper import g", "from .missing import g"))
        broken = tool(root, "verify", str(root), stamp)
        assert broken.returncode == 1 and "lost\t" in broken.stdout, broken.stdout
        put(root, "core/shared/util.py", saved)

        changed = text("core/shared/helper.py")
        put(root, "core/shared/helper.py", changed.replace("return 41", "return 40 + 1"))
        untested = tool(root, "verify", str(root), stamp)
        assert untested.returncode == 1 and "no-test\tcore/shared/helper.py" in untested.stdout, untested.stdout
        put(root, "tests/test_helper.py", "from core.shared import helper\n\nassert helper.g() == 41\n")
        tested = tool(root, "verify", str(root), stamp)
        assert tested.returncode == 0 and "logic-changed\tcore/shared/helper.py" in tested.stdout, tested.stdout
        put(root, "core/new_part.py", "X = 1\n")
        added = tool(root, "verify", str(root), stamp)
        assert added.returncode == 1 and "no-test\tcore/new_part.py" in added.stdout, added.stdout
        (root / "core/new_part.py").unlink()
        (root / "tests/test_helper.py").unlink()
        put(root, "core/shared/helper.py", changed)

        held = tool(root, "undo", str(root), stamp)
        assert held.returncode == 1 and "CHANGED SINCE APPLY" in held.stdout, "undo must not drop later edits silently"
        back = tool(root, "undo", str(root), stamp, "--force")
        assert back.returncode == 0, back.stdout + back.stderr
        assert fingerprint(root) == original, "undo restores every byte"
        assert own_test(root).stdout.strip() == "pass"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in {"scripts/job.py": "X = 1\n", "skill.md": "Run ${ROOT}/scripts/<name>.py or scripts/<name>.py, not myscripts/x.\n", "tests/test_x.py": 'FOLDER = "scripts"\n'}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        out = tool(root, "apply", str(root), plan_file(root.parent / root.name, {"scripts": "tools"}))
        (root / "moves.json").unlink()
        stamp = out.stdout.split("\n")[0].split("\t")[1]
        said = tool(root, "verify", str(root), stamp).stdout
        assert said.count("old-mention\tskill.md:1") == 2 and "old-mention\ttests/test_x.py:1" in said, said
        assert "myscripts" not in said, "a longer word is not a mention"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "a/one.txt").write_text("1\n", encoding="utf-8")
        (root / "b/two.txt").write_text("2\n", encoding="utf-8")
        before = fingerprint(root)
        out = tool(root, "apply", str(root), plan_file(root.parent / root.name, {"a": "b"}))
        (root / "moves.json").unlink()
        assert out.returncode == 0 and (root / "b/one.txt").exists() and (root / "b/two.txt").exists(), out.stdout + out.stderr
        stamp = out.stdout.split("\n")[0].split("\t")[1]
        assert tool(root, "undo", str(root), stamp, "--force").returncode == 0
        assert fingerprint(root) == before and not (root / "b/one.txt").exists(), "undo of a merge touches only what moved"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel, text in {"core/a.py": "X = 1\n", "services/b.py": "from core.a import X\n", "docs/core/x.txt": "doc\n"}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / "tests").mkdir()
        shutil.copyfile(TEMPLATE, root / "tests/test_structure.py")
        rules = {"top": ["core", "services", "docs", "tests"], "no_code_in": ["docs"], "layers": {"core": [], "services": ["core"]}, "forbid": [{"pattern": r"sys\.path\.insert"}]}
        (root / "docs/core/structure.json").write_text(json.dumps(rules), encoding="utf-8")
        check = lambda: subprocess.run([sys.executable, "-I", "-X", "utf8", "tests/test_structure.py"], capture_output=True, text=True, cwd=root)
        assert check().returncode == 0, check().stderr
        for rel, text in {"misc/x.txt": "x\n", "docs/y.py": "Y = 1\n", "core/c.py": "from services.b import X\n", "services/d.py": "import sys\nsys.path.insert(0, '.')\n"}.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            assert check().returncode == 1, rel
            path.unlink()
            if rel.startswith("misc/"):
                path.parent.rmdir()
        assert check().returncode == 0
    print("ok")


if __name__ == "__main__":
    demo()
