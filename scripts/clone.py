#!/usr/bin/env python3
"""A project's own copy of this plugin. Standard library only.

A project's first use copies the plugin to ~/phro-project_<project> and turns it on for that project alone, so what a project builds never enters the original.

  clone.py ensure <project root> [--dest DIR] [--no-register]   make this project's copy if it has none, and say which plugin to use
  clone.py sync [--from DIR]                                    run from a copy: take a newer original, keeping what the project added or changed
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ORIGIN = "origin.json"
MANIFEST = ".claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"
OWN = {ORIGIN, MANIFEST, MARKET}  # written for each copy, so a sync leaves them to this script
IGNORE = {".git", "__pycache__", ".phro-project", "results", ".venv", "node_modules"}
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
            if rel not in OWN:
                yield rel


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


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
    write_json(temporary / ORIGIN, {"origin": original, "origin_dir": str(source), "version": version, "project": str(project), "files": files})
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
    if not found:
        sys.exit(f"FAILED\tno original found near {start}: pass --from")
    return max(found, key=lambda p: tuple(int(n) for n in read_json(p / MANIFEST)["version"].split(".")))


def command_sync(a):
    if not (HERE / ORIGIN).is_file():
        print("FAILED\tthis is the original: run sync from a project's copy")
        sys.exit(1)
    record = read_json(HERE / ORIGIN)
    source = Path(a.source).resolve() if a.source else newest_origin(record)
    old = record["files"]
    files = {}
    counts = {"updated": 0, "added": 0, "removed": 0}
    kept = []
    for rel in files_of(source):
        wanted = digest(source / rel)
        target = HERE / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, target)
            counts["added"] += 1
        else:
            current = digest(target)
            if current != wanted and old.get(rel) != current:  # the project changed it, so it stays
                kept.append(rel)
                if rel in old:
                    files[rel] = old[rel]
                continue
            if current != wanted:
                shutil.copy2(source / rel, target)
                counts["updated"] += 1
        files[rel] = wanted
    for rel, recorded in old.items():
        target = HERE / rel
        if rel in files or rel in kept or not target.exists():
            continue
        if digest(target) == recorded:
            target.unlink()
            counts["removed"] += 1
        else:
            kept.append(rel)
    version = read_json(source / MANIFEST)["version"]
    manifest = read_json(HERE / MANIFEST)
    manifest["version"] = version
    write_json(HERE / MANIFEST, manifest)
    record.update(origin_dir=str(source), version=version, files=files)
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
    p.add_argument("--from", dest="source", help="the original to take; the default is the newest installed")
    p.set_defaults(fn=command_sync)
    a = parser.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
