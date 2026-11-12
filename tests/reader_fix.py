"""Test helpers for the document reader tests."""
import zipfile
from pathlib import Path


def make_zip(path, members, stored=("mimetype",)):
    """Write a zip from {name: str | bytes}; names in `stored` go first, uncompressed."""
    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        names = [n for n in stored if n in members] + [n for n in members if n not in stored]
        for name in names:
            data = members[name]
            if isinstance(data, str):
                data = data.encode("utf-8")
            method = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return path
