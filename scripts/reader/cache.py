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


def _complete(d: Path) -> bool:
    return (d / "out.md").is_file() and (d / "meta.json").is_file()


def store(k: str, src, hidden: bool = False) -> Path:
    bucket = root() / k[:2]
    target = bucket / k
    if _complete(target):
        return target
    with _lock:
        n = next(_counter)
    tmp = bucket / f".tmp-{k}-{os.getpid()}-{n}"
    try:
        convert(src, tmp, hidden)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    for attempt in (0, 1):
        try:
            os.replace(tmp, target)
            return target
        except OSError:
            # on Windows replacing onto an existing directory fails
            if _complete(target):  # another writer won
                shutil.rmtree(tmp, ignore_errors=True)
                return target
            if attempt == 0 and target.exists():  # incomplete leftover: clear and retry once
                shutil.rmtree(target, ignore_errors=True)
                continue
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def prune(days: int = 30) -> int:
    cutoff = time.time() - days * 86400
    n = 0
    base = root()
    for bucket in base.iterdir():
        if not bucket.is_dir():
            continue
        for d in bucket.iterdir():
            if d.name.startswith(".tmp-"):
                try:
                    stale = d.stat().st_mtime < time.time() - 86400
                except OSError:
                    continue
                if stale:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
                continue
            try:
                old = (d / "meta.json").stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
