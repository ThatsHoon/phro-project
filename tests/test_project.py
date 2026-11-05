"""Seeded-defect test: plant one of each defect in a plugin copy, expect each found, and expect nothing on the clean control.

Run: python -I -X utf8 tests/test_project.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/project.py"
sys.path.insert(0, str(SCRIPT.parent))
import project  # noqa: E402

SHARED = "\n".join(f"Shared rule number {i} that was copied into more than one instruction file." for i in range(4))


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def skill(name, description, body):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def grader(name):
    return f"---\ntype: tool_used\ntool: Skill\ninput_match: 'x?{name}\"'\n---\n\nok\n"


def clean(root):
    """A small plugin with nothing wrong: every part reached, every skill tested, descriptions apart."""
    write(root, ".claude-plugin/plugin.json", json.dumps({"name": "demo", "version": "0.1.0"}))
    write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, then run `python scripts/rotate.py`."))
    write(root, "skills/alpha/references/steps.md", "Step one.\n")
    write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha` when a key expired."))
    write(root, "scripts/rotate.py", "import helper\nprint(helper.VALUE)\n")
    write(root, "scripts/helper.py", "VALUE = 1\n")
    for name in ("alpha", "beta"):
        write(root, f"evals/{name}-case/prompt.md", "do the thing\n")
        write(root, f"evals/{name}-case/graders/target.md", grader(name))


def run(defect=None):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        if defect:
            defect(root)
        return project.findings(root), root


def kinds(found):
    return {f[0] for f in found}


def demo():
    found, _ = run()
    assert not found, found

    def broken(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Read `references/gone.md`."))
    assert ("broken-ref", "skills/beta/SKILL.md", "references/gone.md") in run(broken)[0]

    def duplicate(root):
        body = "A long enough body. " * 20
        write(root, "skills/alpha/references/one.md", body)
        write(root, "skills/beta/references/two.md", body)
        write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, `references/one.md`, run `python scripts/rotate.py`."))
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Read `references/two.md`."))
    assert kinds(run(duplicate)[0]) == {"duplicate-file"}

    def copied(root):
        write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, then run `python scripts/rotate.py`.\n" + SHARED))
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`.\n" + SHARED))
    assert kinds(run(copied)[0]) == {"shared-lines"}

    def orphan(root):
        write(root, "scripts/dead.py", "print('never called')\n")
    assert ("orphan", "scripts/dead.py", "no file refers to it") in run(orphan)[0]

    def untested(root):
        write(root, "skills/gamma/SKILL.md", skill("gamma", "Compresses satellite imagery tiles for archive storage.", "Do it."))
    assert kinds(run(untested)[0]) == {"no-eval"}

    def in_route_cases(root):
        untested(root)
        write(root, "tests/route_cases.json", json.dumps([{"id": "c1", "skill": "gamma", "prompt": "compress"}]))
    assert not run(in_route_cases)[0], "a skill with routing cases is tested"

    def only_a_negative(root):
        untested(root)
        write(root, "tests/route_cases.json", json.dumps([{"id": "n1", "skill": None, "prompt": "do not fire gamma"}]))
    assert kinds(run(only_a_negative)[0]) == {"no-eval"}, "a negative case that names a skill does not test it"

    def user_only(root):
        write(root, "skills/gamma/SKILL.md", "---\nname: gamma\ndescription: Compresses satellite imagery tiles for archive storage.\ndisable-model-invocation: true\n---\n\nDo it.\n")
    assert not run(user_only)[0], "routing cannot select a skill only the user invokes"

    def overlap(root):
        write(root, "skills/gamma/SKILL.md", skill("gamma", "Renders invoice PDFs from monthly billing exports for accounting.", "Do it."))
        write(root, "evals/gamma-case/prompt.md", "x\n")
        write(root, "evals/gamma-case/graders/target.md", grader("gamma"))
    assert kinds(run(overlap)[0]) == {"overlap-candidate"}

    def long_description(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoices. " * 100, "Hand off to `demo:alpha`."))
    assert "long-description" in kinds(run(long_description)[0])

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        db = project.scan(root)[0]
        up = {r[0] for r in db.execute("select src from edge where dst = 'scripts/helper.py'")}
        assert up == {"scripts/rotate.py"}, up
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "impact", str(root), "helper.py"], capture_output=True, text=True, encoding="utf-8").stdout
        assert "scripts/rotate.py" in out and "skill:alpha" in out and "skill:beta" in out and "eval:alpha-case" in out, out
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "impact", str(root), "scripts/rotate.py"], capture_output=True, text=True, encoding="utf-8").stdout
        assert "only it uses\tscripts/helper.py" in out, out

        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps([{"id": "c1", "skill": "alpha", "prompt": "rotate"}, {"id": "n1", "skill": None, "prompt": "hello"}]), encoding="utf-8")
        out_dir = Path(d) / "copy"
        args = [sys.executable, "-I", "-X", "utf8", str(SCRIPT), "build", str(root), str(cases), str(out_dir), "--stub"]
        subprocess.run(args, check=True, capture_output=True)
        assert "Stop here" in (out_dir / "skills/alpha/SKILL.md").read_text(encoding="utf-8")
        assert "Read `references" in (root / "skills/alpha/SKILL.md").read_text(encoding="utf-8"), "the plugin itself must stay untouched"
        assert "max: 0" in (out_dir / "evals/n1/graders/target.md").read_text(encoding="utf-8")
        assert (out_dir / "evals/c1/graders/to-beta.md").exists()

        def result(name, target, took, error=None):
            graders = [{"name": "target", "passed": target}] + [{"name": f"to-{n}", "passed": n == took} for n in ("alpha", "beta")]
            spec = [{"name": "target", "config": {"input_match": '"skill"\\s*:\\s*"(?:[\\w-]+:)?alpha"'}}]
            return {"name": name, "graders": spec, "arms": {"with": [{"graders": graders, "error": error}]}}
        cut = "exit 1: Reached maximum number of turns (2)"
        doc = {"costUsd": 1, "partial": False, "cases": [result("c1", True, "alpha"), result("c2", False, "beta", cut), result("c3", False, None, cut), result("c4", False, None)]}
        path = Path(d) / "result.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "target\t1/4" in out and "alpha\t4\t1\t1\t1\t1" in out and "c3 alpha -> unresolved, cut off" in out and "c4 alpha -> none" in out, out
    print("ok")


if __name__ == "__main__":
    demo()
