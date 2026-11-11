"""Content-hash cache: entries live at <root>/<sha1[:2]>/<sha1>/ (out.md, assets/, meta.json)."""
import hashlib
import itertools
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import VERSION, convert

_counter = itertools.count()
_lock = threading.Lock()


def root() -> Path:
    env = os.environ.get("PHRO_READER_CACHE")
    if env:
        base = Path(env)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "phro-project" / "reader"
    else:
        base = Path.home() / ".cache" / "phro-project" / "reader"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        fb = Path(tempfile.gettempdir()) / "phro-project-reader"
        fb.mkdir(parents=True, exist_ok=True)
        return fb


def key(path) -> str:
    h = hashlib.sha1(VERSION.encode() + b"\0")
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def lookup(k: str):
    d = root() / k[:2] / k
    if (d / "out.md").is_file() and (d / "meta.json").is_file():
        try:
            os.utime(d / "meta.json", None)
        except OSError:
            pass
        return d
    return None


def store(k: str, src, hidden: bool = False) -> Path:
    bucket = root() / k[:2]
    target = bucket / k
    if (target / "out.md").is_file() and (target / "meta.json").is_file():
        return target
    with _lock:
        n = next(_counter)
    tmp = bucket / f".tmp-{k}-{os.getpid()}-{n}"
    try:
        convert(src, tmp, hidden)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        os.replace(tmp, target)
    except OSError:
        # on Windows replacing onto an existing directory fails: another writer won
        if target.exists():
            shutil.rmtree(tmp, ignore_errors=True)
            return target
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target


def prune(days: int = 30) -> int:
    cutoff = time.time() - days * 86400
    n = 0
    base = root()
    for bucket in base.iterdir():
        if not bucket.is_dir():
            continue
        for d in bucket.iterdir():
            if d.name.startswith(".tmp-"):
                continue
            try:
                old = (d / "meta.json").stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
