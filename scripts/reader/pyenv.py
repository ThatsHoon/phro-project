"""State of the optional venvs (kind "pdf": pypdfium2; kind "image": resvg_py and Pillow, for figures off
Windows): cheap checks only (no subprocess, no format modules).

A venv is made only by `read.py setup` (see pysetup). Here: where it lives, whether it is usable, and
the engine id a file is read with.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

KINDS = ("pdf", "image")
_HERE = Path(__file__).resolve().parent
REQUIREMENTS = {"pdf": _HERE / "requirements-pdf.txt", "image": _HERE / "requirements-image.txt"}
_ENV = {"pdf": "PHRO_READER_PYENV", "image": "PHRO_READER_IMAGEENV"}
_DISTS = {"pdf": ("pypdfium2",), "image": ("resvg-py", "pillow")}


def _check(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")


def _requirements(kind) -> Path:
    _check(kind)
    return REQUIREMENTS[kind]


def forced_native(kind="pdf") -> bool:
    """The kind's env var is `off`: pdf reads natively, image draws nothing (engine() is "none")."""
    _check(kind)
    return os.environ.get(_ENV[kind]) == "off"


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


def ready_kind(ready) -> str:
    """The kind a ready.json names; one without the field is a 2d pdf venv."""
    return ready.get("kind", "pdf") if isinstance(ready, dict) else "pdf"


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
    _check(kind)
    venv = Path(venv)
    if not venv.is_dir():
        return "MISSING"
    if not interpreter(venv).is_file():
        return "BROKEN"
    ready = _ready(venv)
    if ready is None or ready_kind(ready) != kind:
        return "BROKEN"
    home = _home(venv)
    if not home or not Path(home).is_dir():
        return "BROKEN"
    if ready.get("requirements") != requirements_hash(kind):
        return "STALE"
    return "READY"


def engine_id(version, build) -> str:
    return f"pdfium-{version}-{build}"


def image_engine_id(resvg: str, pillow: str) -> str:
    return f"resvg-{resvg}-pillow-{pillow}"


def engine(kind="pdf", venv=None) -> str:
    """pdf: the engine id when READY, else "native". image: the id `raster.backend()` gives inside the venv when
    READY, else "none"."""
    _check(kind)
    off = {"pdf": "native", "image": "none"}[kind]
    if forced_native(kind):
        return off
    venv = Path(venv) if venv is not None else venv_dir(kind)
    if state(venv, kind) != "READY":
        return off
    ready = _ready(venv)
    if kind == "image":
        r, p = ready.get("resvg"), ready.get("pillow")
        return image_engine_id(r, p) if r and p and isinstance(r, str) and isinstance(p, str) else off
    if not ready.get("pypdfium2") or not ready.get("pdfium"):
        return "native"
    return engine_id(ready["pypdfium2"], ready["pdfium"])


def engine_id_from_module(kind="pdf") -> str:
    """Used in the venv child, where the libraries import."""
    _check(kind)
    if kind == "image":
        import importlib.metadata
        return image_engine_id(importlib.metadata.version("resvg-py"), importlib.metadata.version("pillow"))
    from pypdfium2 import version as v
    return engine_id(v.PYPDFIUM_INFO.version, v.PDFIUM_INFO.build)


def _norm(name) -> str:
    return name.lower().replace("_", "-")


def _records(venv, dist="pypdfium2") -> list:
    """The RECORD files of every dist-info of `dist` under the venv (a pypdfium2 wheel has one; it lists
    pypdfium2_raw too)."""
    found = []
    for rec in Path(venv).rglob("RECORD"):
        info = rec.parent.name
        if info.endswith(".dist-info") and _norm(info[:-len(".dist-info")].rpartition("-")[0]) == _norm(dist):
            found.append(rec)
    return found


def record_digests(venv, kind="pdf") -> dict:
    """{dist: sha256 hex of its RECORD file} for each package of the kind; empty unless every one has exactly one."""
    out = {}
    for dist in _DISTS[kind]:
        recs = _records(venv, dist)
        if len(recs) != 1:
            return {}
        out[dist] = hashlib.sha256(recs[0].read_bytes()).hexdigest()
    return out


def _verify_record(venv, dist, want, needs=None) -> bool:
    """The one RECORD of `dist` hashes to `want`, lists a file under `needs` (when given), and every file it lists
    with a digest (paths are relative to site-packages, so `../../Scripts/...` counts) hashes to it."""
    recs = _records(venv, dist)
    if len(recs) != 1:
        return False
    rec = recs[0]
    try:
        data = rec.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(data).hexdigest() != want:
        return False
    base = rec.parent.parent
    seen = needs is None
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            continue
        if needs and parts[0].startswith(needs):
            seen = True
        if not parts[1].startswith("sha256="):
            continue
        digest = parts[1][len("sha256="):]
        try:
            got = base64.urlsafe_b64encode(hashlib.sha256((base / parts[0]).read_bytes()).digest())
        except OSError:
            return False
        if got.rstrip(b"=").decode("ascii") != digest.rstrip("="):
            return False
    return seen


def verify(venv, kind="pdf") -> bool:
    """The full check, for every package of the kind (see _verify_record)."""
    _check(kind)
    ready = _ready(venv)
    if ready is None or not isinstance(ready.get("records"), dict) or ready_kind(ready) != kind:
        return False
    if kind == "image":
        return all(_verify_record(venv, d, ready["records"].get(d)) for d in _DISTS["image"])
    return _verify_record(venv, "pypdfium2", ready["records"].get("pypdfium2"), needs="pypdfium2_raw/")


def declined(kind="pdf") -> bool:
    _check(kind)
    from . import cache
    return (cache.root() / f"{kind}-declined").exists()

