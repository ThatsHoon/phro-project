"""Which OCR engines this machine has, from their files alone: no engine is loaded and no process is started. The
gate keys every Read with `backend()`, so this module imports only cheap standard modules, and the answer is kept in
a memo under the cache root, checked against the files it rests on.

Tesseract is found on the absolute entries of PATH only, as `tesseract.exe` on Windows: shutil.which and a bare name
given to CreateProcess both look in the current folder first on Windows, which would run a `tesseract.bat` from the
folder of the document being read.
"""
import json
import os
import sys
import time
from pathlib import Path

MEMO = "ocr-probe.json"  # the memo's file under the cache root
_REWALK = 86400  # seconds a PATH walk that found no Tesseract stands before PATH is walked again
_ENV = ("PATH", "PHRO_READER_TESSERACT", "TESSDATA_PREFIX", "PHRO_READER_OCR", "WINDIR", "SystemRoot")


def _stamp(p, deps: dict | None = None) -> str:
    """'<size>:<mtime_ns>' of a file or folder, '0:0' when there is none or it cannot be read; recorded in `deps`."""
    try:
        st = os.stat(p)
        s = f"{st.st_size}:{st.st_mtime_ns}"
    except (OSError, TypeError, ValueError):
        s = "0:0"
    if deps is not None and p is not None:
        deps[str(p)] = s
    return s


# ---- Windows OCR -----------------------------------------------------------------------------------------------------

def _ocr_root() -> Path:
    return Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows") / "OCR"


def dll() -> Path:
    """Where the Windows build keeps Windows.Media.Ocr.dll."""
    return Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows") / "System32" \
        / "Windows.Media.Ocr.dll"


def recognizers(deps: dict | None = None) -> list[str]:
    """The recognizer folders under %WINDIR%\\OCR that hold an .orp file, lower case, found by listing only."""
    if sys.platform != "win32":
        return []
    root = _ocr_root()
    _stamp(root, deps)
    try:
        with os.scandir(root) as it:
            folders = [e for e in it if e.is_dir()]
    except OSError:
        return []
    names = []
    for d in folders:
        _stamp(d.path, deps)
        try:
            with os.scandir(d.path) as files:
                if any(f.name.lower().endswith(".orp") and f.is_file() for f in files):
                    names.append(d.name.lower())
        except OSError:
            continue
    return sorted(names)


def _orp(folder: Path | None) -> Path | None:
    """The folder's first .orp file in sorted order, whatever its name; None when it has none."""
    if folder is None:
        return None
    try:
        with os.scandir(folder) as it:
            names = sorted(e.name for e in it if e.name.lower().endswith(".orp") and e.is_file())
    except OSError:
        return None
    return folder / names[0] if names else None


def _recognizer_folder(tag: str, deps: dict | None = None) -> Path | None:
    """The recognizer folder for a BCP-47 tag: the same name, else one for a region of it, else one of the same
    language; None when there is none."""
    tag = tag.lower()
    folders = recognizers(deps)
    folder = next((f for f in folders if f == tag), None) \
        or next((f for f in folders if f.startswith(tag + "-")), None) \
        or next((f for f in folders if f.split("-")[0] == tag.split("-")[0]), None)
    return None if folder is None else _ocr_root() / folder


def _version(tag: str, deps: dict | None = None) -> str:
    """'<size>:<mtime_ns>/<size>:<mtime_ns>' of Windows.Media.Ocr.dll and of the .orp file of the recognizer for
    `tag`, a part '0:0' when its file is missing or cannot be read: the version the Windows engine reports. Never
    raises: the version only keys caches."""
    try:
        orp = _orp(_recognizer_folder(tag, deps))
    except Exception:  # noqa: BLE001 - a version is never a reason for no engine
        orp = None
    return f"{_stamp(dll(), deps)}/{_stamp(orp, deps)}"


def _win(deps: dict) -> tuple[str, str] | None:
    """`win-ko` with a Korean recognizer, else `win-<language>` of the first recognizer folder, and the version the
    engine reports (`_version`). None without the dll or a recognizer."""
    folders = recognizers(deps)
    lib = dll()
    _stamp(lib, deps)
    if not folders or not lib.is_file():
        return None
    tag = next((f for f in folders if f.split("-")[0] == "ko"), folders[0])
    return "win-" + tag.split("-")[0], _version(tag, deps)


# ---- Tesseract -------------------------------------------------------------------------------------------------------

def find() -> Path | None:
    """`PHRO_READER_TESSERACT` when it is an absolute path to a file, else the first `tesseract` (`tesseract.exe` on
    Windows) on an absolute PATH entry; None otherwise. Nothing is run. A variable that is set but names no file
    gives None: the user pointed somewhere specific."""
    env = os.environ.get("PHRO_READER_TESSERACT")
    if env:
        p = Path(env)
        return p if p.is_absolute() and p.is_file() else None
    win = sys.platform == "win32"
    name = "tesseract.exe" if win else "tesseract"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        cand = os.path.join(entry, name)
        if os.path.isfile(cand) and (win or os.access(cand, os.X_OK)):
            return Path(cand)
    return None


def _newest_first(name: str) -> tuple:
    """A version folder's name as a sort key: numeric parts compared as numbers, other names after them."""
    parts = name.split(".")
    if all(p.isdigit() for p in parts):
        return 1, tuple(int(p) for p in parts)
    return 0, ()


def tessdata(exe, deps: dict | None = None) -> Path | None:
    """TESSDATA_PREFIX when set; else the first folder that exists of <bin>/tessdata, <prefix>/share/tessdata and
    <prefix>/share/tesseract-ocr/<version>/tessdata, newest version first, where <bin> holds the executable and
    <prefix> is its parent (the Windows installer, Homebrew, and Debian or Ubuntu); None when none does."""
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        _stamp(env, deps)
        return Path(env)
    exe = Path(exe)
    share = exe.parent.parent / "share"
    cands = [exe.parent / "tessdata", share / "tessdata"]
    versions = share / "tesseract-ocr"
    _stamp(versions, deps)
    try:
        with os.scandir(versions) as it:
            names = [e.name for e in it if e.is_dir()]
    except OSError:
        names = []
    cands += [versions / n / "tessdata" for n in sorted(names, key=_newest_first, reverse=True)]
    for c in cands:
        _stamp(c, deps)
        if c.is_dir():
            return c
    return None


def _langs(d: Path | None) -> frozenset[str]:
    try:
        return frozenset(p.stem for p in d.glob("*.traineddata")) if d is not None else frozenset()
    except OSError:
        return frozenset()


def langs(exe) -> frozenset[str]:
    """The stems of `*.traineddata` in the tessdata folder, by listing only."""
    return _langs(tessdata(exe))


def version(exe, used, deps: dict | None = None) -> str:
    """`size:mtime_ns` of the executable and of each traineddata used, joined with `/`, in sorted language order."""
    exe = Path(exe)
    d = tessdata(exe, deps)
    parts = [_stamp(exe, deps)]
    for lang in sorted(used):
        parts.append(_stamp(d / f"{lang}.traineddata", deps) if d is not None else "0:0")
    return "/".join(parts)


def _tess(exe: Path | None, deps: dict) -> tuple[str, str] | None:
    """`tess` and the version of the executable `exe` and its kor and eng traineddata; None when there is none or it
    has neither."""
    if exe is None:
        return None
    _stamp(exe, deps)
    if not exe.is_file():
        return None
    used = _langs(tessdata(exe, deps)) & {"kor", "eng"}
    return ("tess", version(exe, used, deps)) if used else None


# ---- the answer ------------------------------------------------------------------------------------------------------

def probe(deps: dict) -> tuple[str, str, bool]:
    """(engine id, version, whether PHRO_READER_TESSERACT is set or the PATH walk found a Tesseract) from the files,
    recording in `deps` the stamp of every file and folder the answer rests on. An engine whose probe fails, for any
    reason, is absent."""
    named = os.environ.get("PHRO_READER_TESSERACT")
    if named:
        _stamp(named, deps)
    try:
        exe = find()
    except Exception:  # noqa: BLE001 - OCR never makes a Read fail
        exe = None
    found = []
    for one in (lambda: _win(deps), lambda: _tess(exe, deps)):
        try:
            got = one()
        except Exception:  # noqa: BLE001 - likewise: that engine is absent
            got = None
        if got is not None:
            found.append(got)
    return "+".join(i for i, _ in found), "+".join(v for _, v in found), bool(named) or exe is not None


def _held(m, env: list) -> tuple[str, str] | None:
    """The memo's answer while it still holds: the same environment, every recorded stamp unchanged, and, when the
    PATH walk found no Tesseract, a walk less than a day old."""
    if not isinstance(m, dict) or m.get("env") != env:
        return None
    ident, ver, deps = m.get("id"), m.get("version"), m.get("deps")
    if not isinstance(ident, str) or not isinstance(ver, str) or not isinstance(deps, dict):
        return None
    walked = m.get("walked")
    if walked is not None:
        if not isinstance(walked, (int, float)) or not 0 <= time.time() - walked < _REWALK:
            return None
    if any(_stamp(p) != s for p, s in deps.items()):
        return None
    return ident, ver


def backend() -> tuple[str, str]:
    """(engine id, version) of the OCR engines on this machine, from their files alone: no engine is loaded and no
    process is started, so the gate can key every Read with it. The id is `win-ko` or `win-<language>`, with `+tess`
    when Tesseract is here too, or `tess` alone; the version joins the engines' versions with `+`. ("", "") for none,
    and under PHRO_READER_OCR=none, where the memo is not used. Any error is an absent engine."""
    if os.environ.get("PHRO_READER_OCR", "").strip().lower() == "none":
        return "", ""
    try:
        from . import cache
        env = [os.environ.get(k, "") for k in _ENV]
        memo = None
        try:
            memo = cache.root() / MEMO
            got = _held(json.loads(memo.read_text(encoding="utf-8")), env)
        except (OSError, ValueError, RecursionError):
            got = None
        if got is not None:
            return got
        deps: dict = {}
        ident, ver, located = probe(deps)
        if memo is not None:
            record = {"env": env, "id": ident, "version": ver, "deps": deps,
                      "walked": None if located else time.time()}
            tmp = memo.with_name(f".{MEMO}.{os.getpid()}")
            try:
                tmp.write_text(json.dumps(record), encoding="utf-8")
                os.replace(tmp, memo)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return ident, ver
    except Exception:  # noqa: BLE001 - OCR never makes a Read fail
        return "", ""
