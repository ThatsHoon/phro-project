"""State of the optional pypdfium2 venv: cheap checks only (no subprocess, no format modules).

The venv is made only by `read.py setup` (see pdfsetup). Here: where it lives, whether it is usable, and
the engine id a pdf is read with.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent / "requirements-pdf.txt"
PACKAGES = ("pypdfium2", "pypdfium2_raw")


def _cache_root() -> Path:
    from . import cache
    return cache.root()


def forced_native() -> bool:
    return os.environ.get("PHRO_READER_PYENV") == "off"


def venv_dir() -> Path:
    env = os.environ.get("PHRO_READER_PYENV")
    if env and env != "off":
        return Path(env)
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data) / "pdf-venv"
    root = _cache_root()
    try:
        ptr = json.loads((root / "pdf-venv.json").read_text(encoding="utf-8"))
        if isinstance(ptr, dict) and isinstance(ptr.get("venv"), str) and ptr["venv"]:
            return Path(ptr["venv"])
    except (OSError, ValueError):
        pass
    return root.parent / "pdf-venv"


def interpreter(venv) -> Path:
    venv = Path(venv)
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python3"


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _ready(venv):
    try:
        data = json.loads((Path(venv) / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _home(venv):
    try:
        for line in (Path(venv) / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().lower() == "home":
                return v.strip()
    except OSError:
        pass
    return None


def state(venv) -> str:
    venv = Path(venv)
    if not venv.is_dir():
        return "MISSING"
    if not interpreter(venv).is_file():
        return "BROKEN"
    ready = _ready(venv)
    if ready is None:
        return "BROKEN"
    home = _home(venv)
    if not home or not Path(home).is_dir():
        return "BROKEN"
    if ready.get("requirements") != requirements_hash():
        return "STALE"
    return "READY"


def engine_id(version, build) -> str:
    return f"pdfium-{version}-{build}"


def engine(venv=None) -> str:
    if forced_native():
        return "native"
    venv = Path(venv) if venv is not None else venv_dir()
    if state(venv) != "READY":
        return "native"
    ready = _ready(venv)
    return engine_id(ready.get("pypdfium2"), ready.get("pdfium"))


def engine_id_from_module() -> str:
    """Used in the venv child, where pypdfium2 imports."""
    from pypdfium2 import version as v
    return engine_id(v.PYPDFIUM_INFO.version, v.PDFIUM_INFO.build)


def _records(venv) -> dict:
    """{package: RECORD path} for the pypdfium2 packages found under the venv."""
    found = {}
    for rec in Path(venv).rglob("RECORD"):
        stem = rec.parent.name
        if rec.parent.suffix != ".dist-info":
            continue
        name = stem[:-len(".dist-info")].rpartition("-")[0]
        if name in PACKAGES:
            found[name] = rec
    return found


def record_digests(venv) -> dict:
    """{package: sha256 hex of its RECORD file}."""
    return {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in _records(venv).items()}


def verify(venv) -> bool:
    """The full check: each RECORD matches ready.json, and every file it lists with a digest hashes to it."""
    ready = _ready(venv)
    if ready is None or not isinstance(ready.get("records"), dict):
        return False
    recs = _records(venv)
    if set(recs) != set(PACKAGES):
        return False
    for name, rec in recs.items():
        try:
            data = rec.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(data).hexdigest() != ready["records"].get(name):
            return False
        base = rec.parent.parent
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.rsplit(",", 2)
            if len(parts) != 3 or not parts[1].startswith("sha256="):
                continue
            want = parts[1][len("sha256="):]
            try:
                got = base64.urlsafe_b64encode(hashlib.sha256((base / parts[0]).read_bytes()).digest())
            except OSError:
                return False
            if got.rstrip(b"=").decode("ascii") != want.rstrip("="):
                return False
    return True


def declined() -> bool:
    return (_cache_root() / "pdf-declined").exists()
