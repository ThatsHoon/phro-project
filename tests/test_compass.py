"""Seeded-defect test: a clean docs/core passes, each planted defect is found, and the hook stays silent without docs/core.

Run: python -I -X utf8 tests/test_compass.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/compass.py"
sys.path.insert(0, str(SCRIPT.parent))
import compass  # noqa: E402

GOALS = "# 목표\n\n## 최종 목표\n\n> 2026-11-05 사용자: 모델 다섯 개를 고성능으로 만든다\n\n## 중기 목표\n\n- 화재 먼저\n"


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
    print("ok")


if __name__ == "__main__":
    demo()
