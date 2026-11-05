"""Seeded-defect test: a clean docs/core passes, each planted defect is found, and the hook stays silent without docs/core.

Run: python -I -X utf8 tests/test_compass.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/compass.py"
sys.path.insert(0, str(SCRIPT.parent))
import compass  # noqa: E402

GOALS = "# 목표\n\n## 최종 목표\n\n> 2026-11-05 사용자: 결제 API 다섯 개를 안정화한다\n\n## 중기 목표\n\n- 정산 먼저\n"


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def clean(root):
    write(root, "src/app.py", "print(1)\n")
    write(root, "docs/core/goals.md", GOALS)
    write(root, "docs/core/features/app.md", "# 앱\n\n진입점은 `src/app.py`다.\n")
    write(root, "docs/core/README.md", "# 핵심\n\n- `goals.md`\n- `features/app.md`\n")
    write(root, "docs/core/core.json", json.dumps({"refresh_days": 1, "docs": [
        {"file": "goals.md", "sources": []},
        {"file": "features/app.md", "sources": ["src/*.py"]},
    ]}))


def kinds(root):
    return {f[0] for f in compass.lint(root)}


def run_hook(root, env_root=True):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if env_root:
        env["CLAUDE_PROJECT_DIR"] = str(root)
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "hook"], input=json.dumps({"cwd": str(root)}), capture_output=True, text=True, encoding="utf-8", env=env)


def demo():
    with tempfile.TemporaryDirectory() as d:
        assert run_hook(d).stdout == "" and run_hook(d).returncode == 0, "silent in a project without docs/core"
        clean(d)
        assert compass.lint(d) == [], compass.lint(d)
        out = run_hook(d, env_root=False).stdout
        assert "`features/app.md`" in out and "structure.md" not in out, out
        write(d, "docs/core/structure.md", "# 구조\n")
        assert "no-manifest" in kinds(d)
        os.remove(Path(d) / "docs/core/structure.md")

        assert compass.stamp(d, []) == []
        manifest = compass.load(d)
        assert manifest["goal_sha"] and len(manifest["reviewed"]) == 2
        assert [r[1] for r in compass.stale(d, manifest)] == [[], []], "nothing changed since the stamp"

        future = time.time() + 60
        os.utime(Path(d) / "src/app.py", (future, future))
        rows = compass.stale(d, manifest)
        assert rows[1][0] == "features/app.md" and len(rows[1][1]) == 1 and not rows[0][1], rows

        write(d, "docs/core/goals.md", GOALS.replace("다섯 개", "세 개"))
        assert "goal-changed" in kinds(d) and compass.stamp(d, []) != []
        assert compass.stamp(d, [], accept_goal=True) == [] and kinds(d) == set()

    planted = {
        "long": lambda d: write(d, "docs/core/features/app.md", "줄\n" * 81),
        "watermark": lambda d: write(d, "docs/core/features/app.md", "Generated with a tool\n"),
        "tell": lambda d: write(d, "docs/core/features/app.md", "앞 — 뒤\n"),
        "history": lambda d: write(d, "docs/core/features/app.md", "## 변경 이력\n"),
        "dated": lambda d: write(d, "docs/core/features/app.md", "작성 2026-10-06\n"),
        "dead-path": lambda d: write(d, "docs/core/features/app.md", "`src/gone.py`를 본다\n"),
        "unlisted": lambda d: write(d, "docs/core/README.md", "# 핵심\n\n- `goals.md`\n"),
        "missing": lambda d: os.remove(Path(d) / "docs/core/goals.md"),
    }
    for kind, plant in planted.items():
        with tempfile.TemporaryDirectory() as d:
            clean(d)
            plant(d)
            assert kind in kinds(d), (kind, compass.lint(d))

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        write(d, "docs/core/README.md", "# 핵심\n\n- `goals.md`\n- `features/app.md`\n" + "줄\n" * 40)
        assert "long" in kinds(d)
        clean(d)
        assert compass.lint(d) == [], "a quoted date in goals.md stays allowed"

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as home, mock.patch.object(compass.Path, "home", return_value=Path(home)):
        clean(d)
        project = Path(home) / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(d).resolve()))
        write(project, "memory/fact.md", "fact\n")
        write(project, "not-memory.md", "a project-folder file that is not a memory\n")
        assert [p.name for p in compass.source_files(d, "~memory", set())] == ["fact.md"], "~memory reads the memory folder only"
        manifest = compass.load(d)
        manifest["reviewed"] = {"goals.md": time.time() - 60}
        manifest["docs"][0]["sources"] = ["~memory"]
        assert [p.name for p in compass.stale(d, manifest)[0][1]] == ["fact.md"], "a changed memory makes its document stale"

        with tempfile.TemporaryDirectory() as other:
            assert run_hook(other).stdout == "", "another project gets nothing from this one"
            assert list(compass.source_files(other, "~memory", set())) == [], "another project reads its own memory only"
            inside = Path(d) / "vendor/other"
            inside.mkdir(parents=True)
            assert run_hook(inside).stdout == "", "a project inside this one gets nothing either"

    with tempfile.TemporaryDirectory() as d:
        write(d, "docs/core/README.md", "# 다른 도구가 만든 문서\n")
        assert run_hook(d).stdout == "", "a docs/core this plugin did not make stays out of the session"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        (Path(d) / "docs/core/README.md").write_bytes("# 핵심 `goals.md` `features/app.md`\n".encode("cp949"))
        out = run_hook(d)
        assert out.returncode == 0 and out.stdout == "" and "Traceback" not in out.stderr, out.stderr

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        write(d, "docs/core/README.md", "# 핵심\n\n- `goals.md`\n- `features/app.md`\n" + "줄\n" * 25000)
        out = run_hook(d).stdout
        assert len(out) < compass.INDEX_CHARS + 500 and "docs/core/README.md" in out, len(out)

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        write(d, "docs/core/goals.md", GOALS.replace("결제 API", "혁신적 결제 API"))
        assert compass.lint(d) == [], "the user's own quote is never flagged"
        write(d, "docs/core/goals.md", GOALS.replace("결제 API", "> 변경 이력 말고 지금 구조만"))
        assert compass.lint(d) == [], "the user's own quote is never flagged"
        write(d, "docs/core/goals.md", GOALS + "\n혁신적 정산\n")
        assert "watermark" in kinds(d), "outside a quote it still counts"

    def status(root):
        return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "status", str(root)], capture_output=True, text=True, encoding="utf-8", check=True).stdout

    def backdate(root, days):
        manifest = compass.load(root)
        old = time.time() - days * 86400
        manifest["reviewed"] = {k: old for k in manifest["reviewed"]}
        if "checked" in manifest:
            manifest["checked"] = old
        compass.save(root, manifest)
        os.utime(Path(root) / "src/app.py", (old - 100, old - 100))

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        compass.stamp(d, [])
        backdate(d, 5)
        assert "refresh may be off" in run_hook(d).stdout, "nothing has checked for five days"
        assert status(d).endswith("STALE\t0\n")
        assert "refresh may be off" not in run_hook(d).stdout, "a quiet project with a running refresh gets no warning"
        backdate(d, 5)
        assert "refresh may be off" in run_hook(d).stdout, "the refresh stopped"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        compass.stamp(d, [])
        write(d, "src/app.py", "print(2)\n")
        future = time.time() + 5
        os.utime(Path(d) / "src/app.py", (future, future))
        status(d)
        manifest = compass.load(d)
        manifest["checked"] = future + 5
        compass.save(d, manifest)
        write(d, "src/new.py", "print(3)\n")
        os.utime(Path(d) / "src/new.py", (future + 10, future + 10))
        assert compass.stamp(d, []) == []
        changed = compass.stale(d, compass.load(d))[1][1]
        assert [p.name for p in changed] == ["new.py"], "a file saved during a refresh was never read, so it stays stale"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        manifest = compass.load(d)
        manifest["docs"][1]["sources"] = ["docs/*.md", "src/*.py"]
        compass.save(d, manifest)
        compass.stamp(d, [])
        status(d)
        future = time.time() + 60
        os.utime(Path(d) / "docs/core/features/app.md", (future, future))
        assert compass.stale(d, compass.load(d))[1][1] == [], "a refresh edits these documents, and that alone never makes them stale"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        manifest = compass.load(d)
        manifest["docs"][0]["sources"] = ["src/*.py"]
        compass.save(d, manifest)
        for number in range(compass.SHOWN + 20):
            write(d, f"src/m{number}.py", "\n")
        said = status(d)
        assert said.count("m0.py") == 1, "a path a second document depends on prints once"
        assert f"{compass.SHOWN + 21} changed" in said and said.count(".py\n") == compass.SHOWN + 21, said[:400]
        assert "21 more not listed" in said and f"{compass.SHOWN} listed above" in said, said[-300:]

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        assert compass.stamp(d, []) == []
        manifest = compass.load(d)
        manifest["docs"][1]["sources"] = ["gone/*.py", "src/*.py", "~memory"]
        compass.save(d, manifest)
        assert [f[2] for f in compass.lint(d) if f[0] == "dead-source"] == ["gone/*.py"], compass.lint(d)

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        assert compass.stamp(d, [], drift="정산에 맞지 않는 작업이 늘었다") == []
        assert "정산에 맞지 않는 작업이 늘었다" in run_hook(d).stdout
        assert compass.stamp(d, []) == [] and "정산에" in run_hook(d).stdout, "a stamp without a report keeps the last one"
        assert compass.stamp(d, [], drift="") == [] and "drift" not in run_hook(d).stdout
        assert compass.stamp(d, [], drift="x" * 900) == [] and len(compass.load(d)["drift"]) == compass.DRIFT_CHARS

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as home, mock.patch.object(compass.Path, "home", return_value=Path(home)):
        clean(d)
        project = Path(home) / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(d).resolve()))
        core = Path(d).resolve() / "docs/core"

        def read(uuid, path):
            return json.dumps({"type": "assistant", "uuid": uuid, "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(path)}}]}}) + "\n"
        write(project, "a.jsonl", read("1", core / "goals.md") + read("2", core / "goals.md") + read("3", Path(d) / "src/app.py"))
        write(project, "fork.jsonl", read("1", core / "goals.md"))
        ran = json.dumps({"type": "assistant", "uuid": "9", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python compass.py stamp ."}}]}}) + "\n"
        write(project, "refresh.jsonl", read("4", core / "features/app.md") + ran)
        write(Path(home) / ".claude/projects/C--other", "b.jsonl", read("5", core / "goals.md"))
        assert compass.usage(d) == ({"goals.md": 2, "features/app.md": 0}, 3, 1), compass.usage(d)

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        write(d, "other/x.py", "\n")
        assert not compass.elsewhere(d, compass.load(d)), "before any review there is no window to count in"
        assert compass.stamp(d, []) == []
        assert not compass.elsewhere(d, compass.load(d)), "nothing changed since the review"
        manifest = compass.load(d)
        manifest["skip"] = ["runs"]
        compass.save(d, manifest)
        write(d, "venv2/pyvenv.cfg", "\n")
        future = time.time() + 60
        for rel in ("demo/twin/a.js", "demo/twin/deep/b.js", "demo/c.js", "top.txt", "src/app.py", "runs/big.bin", "docs/core/extra.txt", "venv2/x.py"):
            write(d, rel, "x\n")
            os.utime(Path(d) / rel, (future, future))
        assert dict(compass.elsewhere(d, compass.load(d))) == {"demo/twin": 2, "demo": 1, ".": 1}, "folders only, without sources, skipped folders, docs/core or a venv"
        for _ in range(2):  # running status by hand must not use up what the next refresh sees
            said = status(d)
            assert "elsewhere\tdemo/twin\t2 changed" in said and "ELSEWHERE\t4\n" in said and said.endswith("STALE\t1\n"), said
        for number in range(compass.SHOWN):
            write(d, f"many/f{number}/x.py", "\n")
        said = status(d)
        assert "and 3 more folders" in said and f"ELSEWHERE\t{compass.SHOWN + 4}\n" in said, said[-200:]
        assert compass.stamp(d, []) == []
        assert not [k for k in compass.elsewhere(d, compass.load(d)) if k.startswith("many")], "a refresh moves the window past what it saw"
        manifest = compass.load(d)
        manifest["reviewed"]["goals.md"] = time.time() - 60
        manifest["reviewed"]["features/app.md"] = time.time() + 600
        assert "many/f0" in compass.elsewhere(d, manifest), "stamping one document must not hide what the others have not seen"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        out = run_hook(d).stdout
        assert "user outranks" in out and "trust the code and report the mismatch" in out and "rules something out" in out, out[:300]
        assert "Reviewed" not in out, "no review yet, so no age to report"
        compass.stamp(d, [])
        assert "Reviewed 0 days ago, last checked for changes 0 days ago" in run_hook(d).stdout
        backdate(d, 5)
        assert "Reviewed 5 days ago, last checked for changes 5 days ago" in run_hook(d).stdout
        status(d)
        out = run_hook(d).stdout
        assert "Reviewed 5 days ago, last checked for changes 0 days ago" in out and "refresh may be off" not in out, "a check is not a review, and the two show apart"

    index = "# 핵심\n\n- `goals.md`\n- `features/app.md`\n"
    hidden = {
        "zero width": "앞\U0000200B뒤\n",
        "bidi": "앞\U0000202E뒤\n",
        "tag characters": "앞\U000E0041\U000E0042뒤\n",
        "html comment": "<!-- 몰래 넣은 지시 -->\n",
        "hidden element": '<div style="color:red; display : none">몰래</div>\n',
        "quoted": "> 사용자 말\U0000200B\n",
    }
    for name, text in hidden.items():
        with tempfile.TemporaryDirectory() as d:
            clean(d)
            write(d, "docs/core/README.md", index + text)
            out = run_hook(d)
            assert out.returncode == 0 and "were not loaded" in out.stdout and "몰래" not in out.stdout and "goals.md" not in out.stdout, (name, out.stdout)
            assert ("hidden", "README.md") in [f[:2] for f in compass.lint(d)], (name, compass.lint(d))
            assert compass.stamp(d, []) != [], "a refresh cannot pass over it"

    with tempfile.TemporaryDirectory() as d:
        clean(d)
        (Path(d) / "docs/core/README.md").write_bytes(b"\xef\xbb\xbf" + index.encode("utf-8"))
        assert "were not loaded" not in run_hook(d).stdout and "`goals.md`" in run_hook(d).stdout, "a byte order mark from a Windows editor is not hidden text"
        assert "hidden" not in kinds(d), "nor does lint call it that"
        write(d, "docs/core/README.md", index + "표시 없는 평범한 한국어, English, 1 < 2 > 1, `<div>` 예시.\n")
        assert "were not loaded" not in run_hook(d).stdout and "hidden" not in kinds(d)
        write(d, "docs/core/features/app.md", "<!-- 색인이 아닌 문서는 세션에 그대로 들어가지 않는다 -->\n")
        assert "hidden" not in kinds(d)
        write(d, "docs/core/features/app.md", "# 앱\n")
        assert compass.stamp(d, [], drift="정산이 밀렸다") == []
        manifest = compass.load(d)
        manifest["drift"] = "정산\U0000200B이 밀렸다"
        compass.save(d, manifest)
        out = run_hook(d).stdout
        assert "`goals.md`" in out and "밀렸다" not in out, "a hidden drift line stays out, the index stays in"
    print("ok")


if __name__ == "__main__":
    demo()
