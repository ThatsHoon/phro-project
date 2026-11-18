"""Checks that make a modular layout portable: import isolation per feature, the copy test, and contract coverage.

Rules live in one JSON file (default contracts/modules.json):
{"features": {"control/hazard": ["shared", "control/geo"], "app/stt": ["shared"], "shared/message": []},
 "consumers": ["training", "examples", "tools"]}
A feature may import itself and the paths listed for it. Nothing in a feature may import a consumer.
Standard library only. Prints paths and counts, never file contents.

  imports   <root> [--rules F]                       every forbidden import, exit 1 if any
  copytest  <root> <feature> [--rules F] [--venv]    copy the feature and what it may import to an empty dir, run its tests
  contracts <root> [--rules F] [--doc F]             contract files per feature, the schema each example names,
                                                     and their link from the contract doc
"""
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKIP = {"__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "weights"}


def load(root, rules):
    data = json.loads((root / rules).read_text(encoding="utf-8"))
    feats = {k.strip("/"): [p.strip("/") for p in v] for k, v in data["features"].items()}
    missing = [f for f in feats if not (root / f).is_dir()]
    if missing:
        sys.exit(f"rules name features that are not folders: {missing}")
    return feats, [c.strip("/") for c in data.get("consumers", [])]


def py_files(folder):
    for p in sorted(folder.rglob("*.py")):
        if not SKIP.intersection(p.relative_to(folder).parts):
            yield p


def imported(path, root):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [(0, "<unparsable>", False)]
    here = path.relative_to(root).with_suffix("").parts[:-1]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name.replace(".", "/"), False) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here[: len(here) - node.level + 1] if node.level > 1 else here
                if node.level - 1 > len(here):
                    out.append((node.lineno, "<above root>", True))
                    continue
                mod = "/".join(base + tuple((node.module or "").split("."))).strip("/")
            else:
                mod = node.module.replace(".", "/")
            out.append((node.lineno, mod, bool(node.level)))
    return out


def under(mod, prefix):
    return mod == prefix or mod.startswith(prefix + "/")


def violations(root, feats, consumers):
    tops = {p.name for p in root.iterdir() if p.is_dir()}
    found = []
    for feat, allowed in feats.items():
        for path in py_files(root / feat):
            for line, mod, relative in imported(path, root):
                rel = f"{path.relative_to(root).as_posix()}:{line}"
                if mod.startswith("<"):
                    found.append(f"{rel} {mod}")
                elif relative and not under(mod, feat):
                    found.append(f"{rel} relative import leaves {feat}: {mod}")
                elif any(under(mod, c) for c in consumers):
                    found.append(f"{rel} imports consumer {mod}")
                elif mod.split("/")[0] in tops and not any(under(mod, a) for a in [feat] + allowed):
                    found.append(f"{rel} {feat} must not import {mod}")
    return found


def cmd_imports(a):
    root = Path(a.root).resolve()
    feats, consumers = load(root, a.rules)
    found = violations(root, feats, consumers)
    for f in found:
        print("FORBIDDEN\t" + f)
    print(f"features\t{len(feats)}\tforbidden\t{len(found)}")
    return 1 if found else 0


def cmd_copytest(a):
    root = Path(a.root).resolve()
    feats, _ = load(root, a.rules)
    feat = a.feature.strip("/")
    if feat not in feats:
        sys.exit(f"{feat} is not a feature in {a.rules}")
    parts = [feat] + feats[feat]
    tmp = Path(tempfile.mkdtemp(prefix="copytest-"))
    try:
        for p in parts:
            shutil.copytree(root / p, tmp / p, ignore=shutil.ignore_patterns(*SKIP))
        for d in {Path(p).parent for p in parts}:  # namespace parents need nothing, but a package parent needs its __init__
            while d != Path("."):
                if (root / d / "__init__.py").exists() and not (tmp / d / "__init__.py").exists():
                    shutil.copy2(root / d / "__init__.py", tmp / d / "__init__.py")
                d = d.parent
        py = sys.executable
        if a.venv:
            subprocess.run([sys.executable, "-m", "venv", str(tmp / ".venv")], check=True)
            py = str(tmp / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            reqs = [tmp / p / "requirements.txt" for p in parts if (tmp / p / "requirements.txt").exists()]
            reqs.append(tmp / "_pytest.txt")
            reqs[-1].write_text("pytest\n", encoding="utf-8")
            for r in reqs:
                if subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(r)]).returncode:
                    print(f"FAIL\tinstall {r.relative_to(tmp).as_posix()}")
                    return 1
        env = dict(os.environ, PYTHONPATH=str(tmp))
        res = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", feat], cwd=tmp, env=env,
                             capture_output=True, text=True)
        tail = ((res.stdout.strip() or res.stderr.strip()).splitlines() or ["no output"])[-1]  # pytest missing speaks on stderr
        print(f"{'OK' if res.returncode == 0 else 'FAIL'}\t{feat}\tcopied {', '.join(parts)}\t{tail}")
        if res.returncode and a.show:
            print(res.stdout[-4000:] + res.stderr[-2000:])
        return 0 if res.returncode == 0 else 1
    finally:
        if a.keep:
            print(f"kept\t{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def schema_links(name, data, schema_dir):
    ref = data.get("schema") if isinstance(data, dict) else None
    files = [ref] if isinstance(ref, str) else list(ref.values()) if isinstance(ref, dict) else []
    if not files:
        return [f"{name} names no schema"]
    return [f"{name} names missing {f}" for f in files if not (schema_dir / str(f)).is_file()]


def cmd_contracts(a):
    root = Path(a.root).resolve()
    feats, _ = load(root, a.rules)
    doc = root / a.doc
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    bad = 0
    for feat in feats:
        c = root / feat / "contract"
        schemas = sorted((c / "schema").glob("*.json")) if c.is_dir() else []
        examples = sorted((c / "examples").glob("*.json")) if c.is_dir() else []
        broken, unlinked = [], []
        for f in schemas + examples + [c / "openapi.json"]:
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError):
                    broken.append(f.relative_to(root).as_posix())
                    continue
                if f in examples:
                    unlinked += schema_links(f.name, data, c / "schema")
        linked = f"{feat}/contract" in text
        problems = (["no schema"] if not schemas else []) + (["no examples"] if not examples else []) + \
                   ([f"bad json {b}" for b in broken]) + unlinked + ([] if linked else [f"not linked from {a.doc}"])
        bad += bool(problems)
        print(f"{'OK' if not problems else 'MISSING'}\t{feat}\tschema {len(schemas)}\texamples {len(examples)}"
              f"\topenapi {'yes' if (c / 'openapi.json').exists() else 'no'}" + ("\t" + "; ".join(problems) if problems else ""))
    print(f"features\t{len(feats)}\tincomplete\t{bad}")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("imports", "copytest", "contracts"):
        p = sub.add_parser(name)
        p.add_argument("root")
        if name == "copytest":
            p.add_argument("feature")
            p.add_argument("--venv", action="store_true", help="fresh venv with the copied requirements.txt files")
            p.add_argument("--keep", action="store_true")
            p.add_argument("--show", action="store_true", help="print the test output tail on failure")
        if name == "contracts":
            p.add_argument("--doc", default="contracts/README.md")
        p.add_argument("--rules", default="contracts/modules.json")
    a = ap.parse_args(argv)
    return {"imports": cmd_imports, "copytest": cmd_copytest, "contracts": cmd_contracts}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
