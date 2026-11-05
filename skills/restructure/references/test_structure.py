"""Folder rules of this project, read from docs/core/structure.json. Standard library only, so it works without any plugin.

Run: python tests/test_structure.py, or through pytest.

structure.json:
{"top": ["core", "services", "docs"], "no_code_in": ["docs"], "layers": {"core": [], "services": ["core"]},
 "forbid": [{"pattern": "sys\\.path\\.insert", "except": ["legacy/"]}]}
"""
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "docs/core/structure.json").read_text(encoding="utf-8"))
SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", ".phro-project", "venv", ".venv"}
CODE = {".py", ".js", ".ts", ".tsx", ".jsx", ".cjs", ".mjs", ".sh", ".ps1"}


def walk(start=ROOT):
    for path in start.iterdir():
        if path.is_dir():
            if path.name not in SKIP and not (path / "pyvenv.cfg").exists():
                yield from walk(path)
        else:
            yield path


def code_files():
    return [p for p in walk() if p.suffix in CODE]


def rel(path):
    return path.relative_to(ROOT).as_posix()


def test_top_level_folders():
    allowed = set(RULES.get("top", []))
    extra = [p.name for p in ROOT.iterdir() if p.is_dir() and p.name not in allowed and p.name not in SKIP and not p.name.startswith(".")]
    assert not extra, f"top-level folders outside docs/core/structure.md: {extra}"


def test_no_code_where_it_does_not_belong():
    banned = tuple(f"{d}/" for d in RULES.get("no_code_in", []))
    found = [rel(p) for p in code_files() if rel(p).startswith(banned)] if banned else []
    assert not found, f"code in a folder that must not hold it: {found[:20]}"


def test_layers():
    layers = RULES.get("layers", {})
    found = []
    for path in code_files():
        name = rel(path)
        here = name.split("/")[0]
        if path.suffix != ".py" or here not in layers:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and not node.level and node.module else []
            for module in modules:
                target = module.split(".")[0]
                if target in layers and target != here and target not in layers[here]:
                    found.append(f"{name}:{node.lineno} {here} must not import {target}")
    assert not found, found[:20]


def test_forbidden_patterns():
    found = []
    for rule in RULES.get("forbid", []):
        pattern = re.compile(rule["pattern"])
        for path in code_files():
            name = rel(path)
            if name.startswith(tuple(rule.get("except", []))) or name.endswith("tests/test_structure.py"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            found += [f"{name}:{n} {rule['pattern']}" for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    assert not found, found[:20]


if __name__ == "__main__":
    for check in (test_top_level_folders, test_no_code_where_it_does_not_belong, test_layers, test_forbidden_patterns):
        check()
    print("ok")
