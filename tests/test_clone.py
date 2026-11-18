import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import clone


def tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def demo():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        home = base / "home"
        home.mkdir()
        env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
        env.pop("CLAUDE_PROJECT_DIR", None)
        original = base / "cache/0.2.5"
        shutil.copytree(ROOT, original, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        (original / ".git").mkdir()
        (original / ".git/config").write_text("x\n", encoding="utf-8")
        (original / ".in_use").mkdir()  # what Claude Code leaves in a plugin folder while a session uses it
        (original / ".in_use/123").write_text('{"pid":123}\n', encoding="utf-8")
        version = json.loads((original / clone.MANIFEST).read_text(encoding="utf-8"))["version"]

        def run(plugin, *args):
            return subprocess.run([sys.executable, "-I", "-X", "utf8", str(plugin / "scripts/clone.py"), *args], capture_output=True, text=True, encoding="utf-8", env=env)

        def ensure(plugin, project):
            return run(plugin, "ensure", str(project), "--no-register")

        def project(rel):
            path = base / rel
            (path / "src").mkdir(parents=True)
            (path / "src/app.py").write_text("print(1)\n", encoding="utf-8")
            return path

        if (original / clone.ORIGIN).is_file():
            (original / clone.ORIGIN).unlink()
            (original / clone.MARKET).unlink(missing_ok=True)
            data = json.loads((original / clone.MANIFEST).read_text(encoding="utf-8"))
            (original / clone.MANIFEST).write_text(json.dumps({**data, "name": "phro-project"}), encoding="utf-8")
            shutil.rmtree(original / ".in_use", ignore_errors=True)

        (original / "mods/.git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (original / ".superpowers/sdd").mkdir(parents=True, exist_ok=True)
        (original / ".superpowers/sdd/progress.md").write_text("log\n", encoding="utf-8")
        (original / ".worktrees/side").mkdir(parents=True, exist_ok=True)
        (original / ".worktrees/side/x.md").write_text("x\n", encoding="utf-8")
        before = tree(original)
        app = project("work/demo-app")
        out = ensure(original, app)
        copy = home / "phro-project_demo-app"
        assert out.returncode == 0 and out.stdout.startswith(f"CREATED\t{copy}"), (out.stdout, out.stderr)
        assert "/reload-plugins" in out.stdout and "phro-project_demo-app:" in out.stdout, out.stdout
        assert tree(original) == before, "the original never changes"
        assert sorted(tree(app)) == ["src/app.py"], "the copy lives outside the project, which gains nothing"

        manifest = json.loads((copy / clone.MANIFEST).read_text(encoding="utf-8"))
        assert manifest["name"] == "phro-project_demo-app" and manifest["version"] == version and "repository" not in manifest, manifest
        market = json.loads((copy / clone.MARKET).read_text(encoding="utf-8"))
        assert market["name"] == manifest["name"] and market["plugins"] == [{"name": manifest["name"], "source": "./", "description": manifest["description"]}], market
        record = json.loads((copy / clone.ORIGIN).read_text(encoding="utf-8"))
        assert Path(record["project"]) == app.resolve() and record["origin"] == "phro-project" and record["version"] == version and record["daily"] is False, record
        assert not (copy / ".git").exists(), "a copy shares no history with the original"
        assert not (copy / "mods/.git").exists(), "a .git pointer file would make git in the copy act on the original's repository"
        assert not (copy / ".superpowers").exists() and not (copy / ".worktrees").exists(), "work logs and worktrees are not the plugin"
        assert not (copy / ".in_use").exists(), "nor Claude Code's in-use marks on the original"
        same = [rel for rel in before if not rel.startswith((".git/", ".superpowers/", ".worktrees/", ".in_use/")) and rel not in clone.OWN and rel != "mods/.git"]
        assert same and all((copy / rel).read_bytes() == (original / rel).read_bytes() for rel in same), "a copy is the plugin as it was"

        assert ensure(original, app).stdout.startswith(f"EXISTS\t{copy}"), "one project, one copy"
        assert [p.name for p in home.iterdir()] == ["phro-project_demo-app"]

        twin = project("other/demo-app")
        out = ensure(original, twin)
        assert out.returncode == 0 and "phro-project_demo-app-" in out.stdout.split("\t")[1], out.stdout
        assert len(list(home.iterdir())) == 2, "two projects with one folder name stay apart"
        assert json.loads((copy / clone.ORIGIN).read_text(encoding="utf-8"))["project"] == str(app.resolve()), "and the first keeps its copy"

        korean = project("work/위성 재난")
        out = ensure(original, korean)
        made = Path(out.stdout.split("\t")[1])
        assert out.returncode == 0 and made.name.startswith("phro-project_") and made.name.isascii() and clone.NAME.fullmatch(made.name), made.name

        assert ensure(original, home).returncode == 1 and ensure(original, home).stdout.startswith("NO-PROJECT"), "the home folder is not a project"
        count = len(list(home.iterdir()))
        out = ensure(original, original)
        assert out.stdout.startswith("ORIGINAL") and len(list(home.iterdir())) == count, "working on the plugin itself makes no copy"

        out = ensure(copy, app)
        assert out.returncode == 0 and out.stdout.startswith(f"THIS\t{copy}"), out.stdout
        out = ensure(copy, twin)
        assert out.returncode == 1 and out.stdout.startswith("WRONG-PROJECT"), "a copy serves its own project only"
        out = run(original, "sync")
        assert out.returncode == 1 and out.stdout.startswith("FAILED"), "only a copy syncs, and it says so"

        def hook(plugin, target):
            return subprocess.run([sys.executable, "-I", "-X", "utf8", str(plugin / "scripts/compass.py"), "hook"], input=json.dumps({"cwd": str(target)}), capture_output=True, text=True, encoding="utf-8", env=env).stdout

        (app / "docs/core").mkdir(parents=True)
        (app / "docs/core/README.md").write_text("# 핵심\n\n- `goals.md`\n", encoding="utf-8")
        (app / "docs/core/core.json").write_text(json.dumps({"refresh_days": 1, "docs": [{"file": "goals.md", "sources": []}], "reviewed": {"goals.md": 1000.0}, "checked": 1000.0}), encoding="utf-8")
        said = hook(copy, app)
        assert f"/{manifest['name']}:compass" in said and "/phro-project:compass" not in said, "a copy names itself"
        assert "newer" not in said, "nothing newer yet"
        (original / ".in_use/456").write_text('{"pid":456}\n', encoding="utf-8")
        assert "newer" not in hook(copy, app), "a session opening on the original is no new original"
        assert "/phro-project:compass" in hook(original, app), "the original names itself"

        newer = base / "cache/9.0.0"
        shutil.copytree(original, newer, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        data = json.loads((newer / clone.MANIFEST).read_text(encoding="utf-8"))
        data["version"] = "9.0.0"
        (newer / clone.MANIFEST).write_text(json.dumps(data), encoding="utf-8")
        with (newer / "skills/design/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nA new line in the original.\n")
        with (newer / "skills/audit/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nThe original changed this too.\n")
        (newer / "skills/fresh").mkdir()
        (newer / "skills/fresh/SKILL.md").write_text("---\nname: fresh\n---\nNew.\n", encoding="utf-8")
        (newer / "skills/route-test/SKILL.md").unlink()

        (copy / "skills/own").mkdir()
        own = "---\nname: own\n---\nThe project's skill.\n"
        (copy / "skills/own/SKILL.md").write_text(own, encoding="utf-8")
        with (copy / "skills/audit/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nThe project changed this.\n")
        changed = (copy / "skills/audit/SKILL.md").read_text(encoding="utf-8")
        said = hook(copy, app)
        assert "9.0.0 is newer" in said and "phro-project_demo-app update" in said, "a copy hears of a newer original, and how to take it"

        before_newer = tree(newer)
        out = run(copy, "sync")
        assert out.returncode == 0, out.stderr
        assert tree(newer) == before_newer, "the original never changes"
        assert "A new line in the original." in (copy / "skills/design/SKILL.md").read_text(encoding="utf-8"), out.stdout
        assert (copy / "skills/fresh/SKILL.md").exists() and not (copy / "skills/route-test/SKILL.md").exists(), out.stdout
        assert (copy / "skills/own/SKILL.md").read_text(encoding="utf-8") == own, "what the project added stays"
        assert (copy / "skills/audit/SKILL.md").read_text(encoding="utf-8") == changed, out.stdout
        assert "kept-changed\tskills/audit/SKILL.md\n" in out.stdout, "both changed it: the copy keeps its own and is told the original moved"
        assert "kept\tskills/own/SKILL.md" not in out.stdout, "a file only the project has is its own, not a kept one"
        manifest = json.loads((copy / clone.MANIFEST).read_text(encoding="utf-8"))
        assert manifest["version"] == "9.0.0" and manifest["name"] == "phro-project_demo-app", manifest
        assert "SYNCED\t9.0.0" in out.stdout and "/reload-plugins" in out.stdout, out.stdout
        assert "newer" not in hook(copy, app) and "9.0.0" not in hook(copy, app)

        after = tree(copy)
        out = run(copy, "sync")
        assert tree(copy) == {**after, clone.ORIGIN: tree(copy)[clone.ORIGIN]} and "kept-changed\tskills/audit/SKILL.md" in out.stdout, "a second sync changes nothing, and still names the change not taken"
        merged = run(copy, "merged", "skills/audit/SKILL.md", "skills/own/SKILL.md")
        assert "MERGED\tskills/audit/SKILL.md" in merged.stdout and "SKIPPED\tskills/own/SKILL.md" in merged.stdout, merged.stdout
        dry = run(copy, "sync", "--dry-run")
        assert "keep\tskills/audit/SKILL.md\n" in dry.stdout and "keep-changed" not in dry.stdout, "once merged, only a later change of the original is named"
        assert "pending" not in json.loads((copy / clone.ORIGIN).read_text(encoding="utf-8"))
        assert "updated\t0" in out.stdout and "added\t0" in out.stdout and "removed\t0" in out.stdout, out.stdout

        (newer / "skills/design/SKILL.md").write_text("restored\n", encoding="utf-8")
        assert "9.0.0 is newer" in hook(copy, app), "an original changed in place is newer, with no new version number"
        with (copy / "skills/design/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nA second project edit.\n")
        (newer / "skills/fresh/SKILL.md").unlink()
        with (copy / "skills/fresh/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nThe project grew this.\n")
        out = run(copy, "sync")
        assert "restored" not in (copy / "skills/design/SKILL.md").read_text(encoding="utf-8"), "a file the project edited is never overwritten"
        assert (copy / "skills/fresh/SKILL.md").exists() and "kept-removed\tskills/fresh/SKILL.md" in out.stdout, "nor deleted, and the removal is named"
        assert "kept-changed\tskills/design/SKILL.md" in out.stdout, out.stdout

        repo = base / "repo"
        shutil.copytree(original, repo, ignore=shutil.ignore_patterns(".git", "__pycache__", ".superpowers", ".worktrees"))

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "one")
        git("worktree", "add", "-q", str(base / "repo-wt"), "-b", "side")
        with (repo / "skills/audit/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("\nNot committed.\n")
        gapp = project("work/git-app")
        shutil.copytree(app / "docs", gapp / "docs")
        out = ensure(base / "repo-wt", gapp)
        gcopy = home / "phro-project_git-app"
        assert out.returncode == 0 and gcopy.is_dir(), (out.stdout, out.stderr)
        record = json.loads((gcopy / clone.ORIGIN).read_text(encoding="utf-8"))
        assert Path(record["origin_repo"]) == repo.resolve() and record["origin_ref"] == "main" and record["origin_id"] == git("rev-parse", "main"), record
        assert "Not committed." not in (gcopy / "skills/audit/SKILL.md").read_text(encoding="utf-8"), "a copy takes what was committed, not a working tree"
        assert not (gcopy / ".git").exists() and "newer" not in hook(gcopy, gapp)

        (repo / "skills/audit/SKILL.md").write_text("Committed later.\n", encoding="utf-8")
        git("commit", "-q", "-am", "two")
        said = hook(gcopy, gapp)
        assert git("rev-parse", "--short=7", "main") in said and "newer" in said and "phro-project_git-app update" in said, said
        before_dry = tree(gcopy)
        out = run(gcopy, "sync", "--dry-run")
        assert out.returncode == 0 and "update\tskills/audit/SKILL.md" in out.stdout and "DRY-RUN" in out.stdout, out.stdout
        assert tree(gcopy) == before_dry, "a dry run writes nothing"
        shutil.rmtree(base / "repo-wt")
        out = run(gcopy, "sync")
        assert out.returncode == 0 and (gcopy / "skills/audit/SKILL.md").read_text(encoding="utf-8") == "Committed later.\n", (out.stdout, out.stderr)
        assert "newer" not in hook(gcopy, gapp), "the worktree it came from is gone, and the copy still follows the repository"
        two = git("rev-parse", "--short=7", "main")
        with (gcopy / "skills/audit/SKILL.md").open("a", encoding="utf-8") as f:
            f.write("The project's line.\n")
        (repo / "skills/audit/SKILL.md").write_text("Committed later.\nAnd again.\n", encoding="utf-8")
        git("commit", "-q", "-am", "three")
        out = run(gcopy, "sync")
        three = git("rev-parse", "--short=7", "main")
        assert f"kept-changed\tskills/audit/SKILL.md\t{two}..{three}" in out.stdout, "the range to read is the original's own history"
        assert "And again." not in (gcopy / "skills/audit/SKILL.md").read_text(encoding="utf-8")

        calls = []

        def fake(args, **kw):
            calls.append((args[1:], kw.get("cwd")))
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(clone.subprocess, "run", fake), mock.patch.object(clone.shutil, "which", lambda name: "claude"):
            assert clone.register(app, copy, "phro-project") == []
        name = "phro-project_demo-app"
        assert [c[0] for c in calls] == [
            ["plugin", "marketplace", "add", str(copy), "--scope", "local"],
            ["plugin", "install", f"{name}@{name}", "--scope", "local"],
            ["plugin", "disable", "phro-project", "--scope", "local"],
        ], calls
        assert all(c[1] == app for c in calls), "the settings written are this project's"

        def refuses(args, **kw):
            return subprocess.CompletedProcess(args, 1, "", "no such thing")
        with mock.patch.object(clone.subprocess, "run", refuses), mock.patch.object(clone.shutil, "which", lambda name: "claude"):
            failed = clone.register(app, copy, "phro-project")
        assert len(failed) == 3 and "no such thing" in failed[0], "a failed step is reported, never swallowed"

        def already(args, **kw):
            return subprocess.CompletedProcess(args, 1, "", "Plugin is already disabled at local scope")
        with mock.patch.object(clone.subprocess, "run", already), mock.patch.object(clone.shutil, "which", lambda name: "claude"):
            assert clone.register(app, copy, "phro-project") == [], "a second ensure finds its own earlier work, which is no failure"
        with mock.patch.object(clone.shutil, "which", lambda name: None):
            assert clone.register(app, copy, "phro-project"), "no command line, no silent success"
    print("ok")


if __name__ == "__main__":
    demo()
