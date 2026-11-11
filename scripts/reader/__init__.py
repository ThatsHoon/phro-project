"""Stdlib-only document reader. Heavier imports happen inside functions."""
from pathlib import Path

VERSION = "0.1.0"

# extension -> module name under reader.formats
FORMATS = {
    "hwpx": "hwpx", "hwp": "hwp",
    "odt": "odf", "ods": "odf", "odp": "odf",
    "docm": "ooxml", "pptm": "ooxml", "ppsx": "ooxml",
    "vsdx": "vsdx", "epub": "epub", "xps": "xps",
    "doc": "doc", "xls": "xls", "ppt": "ppt", "xlsb": "xlsb",
    "rtf": "rtf", "wpd": "wpd",
    "mobi": "mobi", "azw3": "mobi",
    "pages": "iwork", "numbers": "iwork", "key": "iwork",
    "djvu": "djvu",
    "mdb": "jet", "accdb": "jet",
    "pub": "pub", "vsd": "vsd", "mpp": "mpp",
}

# extensions with a working parser; each format task adds its own
BUILT = set()

# handled by anthropic-skills:*, never touched here
SKILL_EXTS = frozenset({"pdf", "docx", "pptx", "xlsx", "xlsm"})


def ext_of(path) -> str:
    return Path(path).suffix.lower().lstrip(".")
