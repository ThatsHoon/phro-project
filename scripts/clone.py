#!/usr/bin/env python3
"""A project's own copy of this plugin. Standard library only.

A project's first use copies the plugin to ~/phro-project_<project> and turns it on for that project alone, so what a project builds never enters the original.

  clone.py ensure <project root> [--dest DIR] [--no-register]   make this project's copy if it has none, and say which plugin to use
  clone.py sync [--from DIR] [--dry-run]                        run from a copy: take a newer original, keeping what the project added or changed

Updates flow one way, from the original into a copy. What a project adds to or changes in its copy is its own and never goes back.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ORIGIN = "origin.json"
MANIFEST = ".claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"
OWN = {ORIGIN, MANIFEST, MARKET}  # written for each copy, so a sync leaves them to this script
IGNORE = {".git", ".in_use", "__pycache__", ".phro-project", "results", ".venv", "node_modules", ".superpowers", ".worktrees"}  # folder or file names; a worktree's .git is a file
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
VERSION = re.compile(r"\d+\.\d+\.\d+")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def files_of(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for name in files:
            rel = (Path(folder) / name).relative_to(root).as_posix()
            if rel not in OWN and name not in IGNORE:
                yield rel


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def tree_id(root):
    """One hash for every file a copy would take, so a changed original shows without a new version number."""
    h = hashlib.sha256()
    for rel in sorted(files_of(root)):
        h.update(f"{rel}\0{digest(root / rel)}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def git(root, *args):
    if not shutil.which("git"):
        return None
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.stdout.strip() if done.returncode == 0 else None


def git_origin(root):
    """The repository and its published ref, when the original is the top of a git checkout, so a copy follows what was committed, not a worktree that may go away."""
    if git(root, "rev-parse", "--show-prefix") != "":
        return None
    repo = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    ref = next((r for r in ("origin/main", "main") if git(repo, "rev-parse", "--verify", "-q", r)), None)
    return (repo, ref) if ref else None


def export(repo, ref, dest):
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def short_hash(project):
    return hashlib.sha256(os.path.normcase(str(project)).encode("utf-8")).hexdigest()[:6]


def place_for(project, parent, original):
    """This project's folder under parent, and whether its copy is already there. A folder name two projects share gets a hash, so neither can reach the other's copy."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-._") or f"project-{short_hash(project)}"
    base = f"{original}_{slug}"
    for folder in (parent / base, parent / f"{base}-{short_hash(project)}"):
        if not folder.exists():
            return folder, False
        if (folder / ORIGIN).is_file() and same_path(read_json(folder / ORIGIN)["project"], project):
            return folder, True
    sys.exit(f"FAILED\tneither {base} nor its hashed name is free under {parent}: pass --dest")


def create(project, folder, source=HERE):
    """A copy that loads in place: its own marketplace at its root, with the plugin at ./, so an edit needs only /reload-plugins."""
    found = git_origin(source)
    if found:
        with tempfile.TemporaryDirectory() as scratch:
            export(*found, scratch)
            return make(project, folder, Path(scratch), {"origin_dir": str(source), "origin_repo": str(found[0]), "origin_ref": found[1],
                                                         "origin_id": git(found[0], "rev-parse", found[1])})
    return make(project, folder, source, {"origin_dir": str(source), "origin_id": tree_id(source)})


def make(project, folder, source, where):
    temporary = folder.with_name(folder.name + ".tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    files = {}
    for rel in files_of(source):
        target = temporary / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
        files[rel] = digest(target)
    manifest = read_json(source / MANIFEST)
    original, version = manifest["name"], manifest["version"]
    manifest.update(name=folder.name, description=f"This project's own copy of {original}. {manifest['description']}")
    for key in ("homepage", "repository"):
        manifest.pop(key, None)
    (temporary / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    write_json(temporary / MANIFEST, manifest)
    owner = manifest.get("author") or {"name": original}
    write_json(temporary / MARKET, {"name": folder.name, "owner": owner, "description": f"Holds this project's own copy of {original}.", "plugins": [{"name": folder.name, "source": "./", "description": manifest["description"]}]})
    write_json(temporary / ORIGIN, {"origin": original, **where, "version": version, "project": str(project), "files": files})
    temporary.rename(folder)


def register(project, folder, original):
    """Turn the copy on and the original off, in this project's own settings. Returns what failed."""
    claude = shutil.which("claude")
    if not claude:
        return ["the claude command was not found"]
    failed = []
    for args in (["plugin", "marketplace", "add", str(folder), "--scope", "local"],
                 ["plugin", "install", f"{folder.name}@{folder.name}", "--scope", "local"],
                 ["plugin", "disable", original, "--scope", "local"]):
        done = subprocess.run([claude, *args], cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
        said = (done.stdout + done.stderr).strip()
        if done.returncode and "already" not in said:  # a second ensure finds its own earlier work
            failed.append(f"claude {' '.join(args)}: {said[:300]}")
    return failed


def command_ensure(a):
    project = Path(a.project).resolve()
    manifest = read_json(HERE / MANIFEST)
    if (HERE / ORIGIN).is_file():
        record = read_json(HERE / ORIGIN)
        if same_path(record["project"], project):
            print(f"THIS\t{HERE}\tthis project's own copy is running: continue, and put what you build for this project under this folder")
            return
        print(f"WRONG-PROJECT\t{HERE}\tthis copy belongs to {record['project']}: stop, and tell the user")
        sys.exit(1)
    if (project / MANIFEST).is_file() and read_json(project / MANIFEST).get("name") == manifest["name"]:
        print(f"ORIGINAL\t{project}\tthis is the plugin's own folder: work here, with no copy")
        return
    if same_path(project, Path.home()) or project.parent == project:
        print(f"NO-PROJECT\t{project}\tnot a project folder: stop, and ask the user to open a session in the project")
        sys.exit(1)
    folder, found = place_for(project, Path(a.dest).resolve() if a.dest else Path.home(), manifest["name"])
    if not found:
        create(project, folder)
    failed = [] if a.no_register else register(project, folder, manifest["name"])
    state = "EXISTS" if found else "CREATED"
    for line in failed:
        print(f"register-failed\t{line}")
    print(f"{state}\t{folder}\tthis project's own copy. Finish this task with these instructions, and put what you build for this project under that folder, never in the original. "
          f"Then tell the user to type /reload-plugins, after which this project uses /{folder.name}:<skill>")


def newest_origin(record):
    start = Path(record["origin_dir"])
    found = [start] if (start / MANIFEST).is_file() else []
    if start.parent.is_dir():
        found += [p for p in start.parent.iterdir() if VERSION.fullmatch(p.name) and (p / MANIFEST).is_file()]
    return max(found, key=lambda p: tuple(int(n) for n in read_json(p / MANIFEST)["version"].split(".")), default=None)


def repo_of(record):
    """A record's git original, also for a copy made before records named one."""
    if record.get("origin_repo"):
        return Path(record["origin_repo"]), record["origin_ref"]
    return git_origin(Path(record["origin_dir"])) if Path(record["origin_dir"]).is_dir() else None


def newer(record):
    """What is ahead of this copy, or None: a new commit on the original's ref, or an original folder that changed or has a newer version."""
    found = repo_of(record)
    if found:
        commit = git(found[0], "rev-parse", found[1])
        return commit[:7] if commit and commit != record.get("origin_id") else None
    source = newest_origin(record)
    if source is None:
        return None
    if not same_path(source, record["origin_dir"]) or tree_id(source) != record.get("origin_id"):
        return read_json(source / MANIFEST)["version"]
    return None


def command_sync(a):
    if not (HERE / ORIGIN).is_file():
        print("FAILED\tthis is the original: run sync from a project's copy")
        sys.exit(1)
    record = read_json(HERE / ORIGIN)
    with tempfile.TemporaryDirectory() as scratch:
        found = None if a.source else repo_of(record)
        if found:
            export(*found, scratch)
            source = Path(scratch)
            where = {"origin_repo": str(found[0]), "origin_ref": found[1], "origin_id": git(found[0], "rev-parse", found[1])}
        else:
            source = Path(a.source).resolve() if a.source else newest_origin(record)
            if source is None:
                sys.exit(f"FAILED\tno original found near {record['origin_dir']}: pass --from")
            for key in ("origin_repo", "origin_ref"):
                record.pop(key, None)
            where = {"origin_dir": str(source), "origin_id": tree_id(source)}
        take(record, source, where, a.dry_run)


def take(record, source, where, dry_run):
    """Bring source into this copy. Files the project added or changed stay as they are."""
    old = record["files"]
    files = {}
    counts = {"updated": 0, "added": 0, "removed": 0}
    kept = []
    planned = []

    def act(kind, rel):
        counts[kind] += 1
        planned.append(f"{kind[:-1] if kind != 'added' else 'add'}\t{rel}")
        if dry_run:
            return
        if kind == "removed":
            (HERE / rel).unlink()
        else:
            (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, HERE / rel)
    for rel in files_of(source):
        wanted = digest(source / rel)
        target = HERE / rel
        if not target.exists():
            act("added", rel)
        else:
            current = digest(target)
            if current != wanted and old.get(rel) != current:  # the project changed it, so it stays
                kept.append(rel)
                if rel in old:
                    files[rel] = old[rel]
                continue
            if current != wanted:
                act("updated", rel)
        files[rel] = wanted
    for rel, recorded in old.items():
        target = HERE / rel
        if rel in files or rel in kept or not target.exists():
            continue
        if any(part in IGNORE for part in rel.split("/")) or digest(target) == recorded:
            act("removed", rel)
        else:
            kept.append(rel)
    version = read_json(source / MANIFEST)["version"]
    if dry_run:
        for line in planned:
            print(line)
        for rel in sorted(kept):
            print(f"keep\t{rel}")
        print(f"DRY-RUN\t{version}\tnothing was written; run sync without --dry-run to take it")
        return
    manifest = read_json(HERE / MANIFEST)
    manifest["version"] = version
    write_json(HERE / MANIFEST, manifest)
    record.update(**where, version=version, files=files)
    write_json(HERE / ORIGIN, record)
    for name, number in counts.items():
        print(f"{name}\t{number}")
    for rel in sorted(kept):
        print(f"kept\t{rel}")
    print(f"SYNCED\t{version}\ttell the user to type /reload-plugins")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ensure", help="make this project's copy if it has none, and say which plugin to use")
    p.add_argument("project")
    p.add_argument("--dest", help="the folder that holds the copy; the default is the home folder")
    p.add_argument("--no-register", action="store_true", help="make the copy without changing any settings")
    p.set_defaults(fn=command_ensure)
    p = sub.add_parser("sync", help="take a newer original, keeping what the project added or changed")
    p.add_argument("--from", dest="source", help="the original to take; the default is the original's published ref, or the newest installed")
    p.add_argument("--dry-run", action="store_true", help="list what would change and write nothing")
    p.set_defaults(fn=command_sync)
    a = parser.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
