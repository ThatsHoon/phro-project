"""State of the optional venvs (kind "pdf": pypdfium2; "mpp": added later): cheap checks only (no subprocess,
no format modules).

A venv is made only by `read.py setup` (see pysetup). Here: where it lives, whether it is usable, and
the engine id a file is read with.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

KINDS = ("pdf", "mpp")
REQUIREMENTS = {"pdf": Path(__file__).resolve().parent / "requirements-pdf.txt"}  # Task 4 adds mpp
_ENV = {"pdf": "PHRO_READER_PYENV", "mpp": "PHRO_READER_MPPENV"}


def _check(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")


def _requirements(kind) -> Path:
    _check(kind)
    try:
        return REQUIREMENTS[kind]
    except KeyError:
        raise ValueError(f"{kind} is not available yet") from None


def forced_native() -> bool:
    return os.environ.get("PHRO_READER_PYENV") == "off"


def venv_dir(kind="pdf") -> Path:
    _check(kind)
    env = os.environ.get(_ENV[kind])
    if env and env != "off":
        return Path(env)
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data) / f"{kind}-venv"
    from . import cache
    root = cache.root()
    try:
        ptr = json.loads((root / f"{kind}-venv.json").read_text(encoding="utf-8"))
        if isinstance(ptr, dict) and isinstance(ptr.get("venv"), str) and ptr["venv"]:
            return Path(ptr["venv"])
    except (OSError, ValueError):
        pass
    return cache.venv_base() / f"{kind}-venv"


def interpreter(venv) -> Path:
    venv = Path(venv)
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python3"


def requirements_hash(kind="pdf") -> str:
    return hashlib.sha256(_requirements(kind).read_bytes()).hexdigest()


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


def state(venv, kind="pdf") -> str:
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
    if ready.get("requirements") != requirements_hash(kind):
        return "STALE"
    return "READY"


def engine_id(version, build) -> str:
    return f"pdfium-{version}-{build}"


def engine(kind="pdf", venv=None) -> str:
    _check(kind)
    if kind == "pdf" and forced_native():
        return "native"
    venv = Path(venv) if venv is not None else venv_dir(kind)
    if state(venv, kind) != "READY":
        return "native"
    ready = _ready(venv)
    if not ready.get("pypdfium2") or not ready.get("pdfium"):
        return "native"
    return engine_id(ready["pypdfium2"], ready["pdfium"])


def engine_id_from_module(kind="pdf") -> str:
    """Used in the venv child, where pypdfium2 imports."""
    from pypdfium2 import version as v
    return engine_id(v.PYPDFIUM_INFO.version, v.PDFIUM_INFO.build)


def _records(venv) -> list:
    """The RECORD files of every pypdfium2 dist-info under the venv (the wheel has one; it lists pypdfium2_raw too)."""
    found = []
    for rec in Path(venv).rglob("RECORD"):
        info = rec.parent.name
        if info.endswith(".dist-info") and info[:-len(".dist-info")].rpartition("-")[0] == "pypdfium2":
            found.append(rec)
    return found


def record_digests(venv) -> dict:
    """{"pypdfium2": sha256 hex of its RECORD file}, empty unless there is exactly one."""
    recs = _records(venv)
    if len(recs) != 1:
        return {}
    return {"pypdfium2": hashlib.sha256(recs[0].read_bytes()).hexdigest()}


def verify(venv, kind="pdf") -> bool:
    """The full check: the one RECORD matches ready.json, lists a pypdfium2_raw file, and every file it lists with
    a digest (paths are relative to site-packages, so `../../Scripts/...` counts) hashes to it."""
    ready = _ready(venv)
    if ready is None or not isinstance(ready.get("records"), dict):
        return False
    recs = _records(venv)
    if len(recs) != 1:
        return False
    rec = recs[0]
    try:
        data = rec.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(data).hexdigest() != ready["records"].get("pypdfium2"):
        return False
    base = rec.parent.parent
    raw = False
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            continue
        if parts[0].startswith("pypdfium2_raw/"):
            raw = True
        if not parts[1].startswith("sha256="):
            continue
        want = parts[1][len("sha256="):]
        try:
            got = base64.urlsafe_b64encode(hashlib.sha256((base / parts[0]).read_bytes()).digest())
        except OSError:
            return False
        if got.rstrip(b"=").decode("ascii") != want.rstrip("="):
            return False
    return raw


def declined(kind="pdf") -> bool:
    _check(kind)
    from . import cache
    return (cache.root() / f"{kind}-declined").exists()
