"""A resumed session keeps what SessionStart printed before, so printing the same text again adds it twice."""
import hashlib
import os
import re
import time
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z0-9_-]+")
KEEP_DAYS = 14


def folder():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "phro-project/seen"


def needed(session_id, part, text, source):
    if not text or not session_id or not TOKEN.fullmatch(session_id):
        return bool(text)
    path = folder() / f"{session_id}.{part}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        if source == "resume" and path.read_text(encoding="ascii") == digest:
            return False
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest, encoding="ascii")
    except OSError:
        pass
    return True


def prune(days=KEEP_DAYS):
    cutoff = time.time() - days * 86400
    base = folder()
    for path in base.glob("*.*") if base.is_dir() else []:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
