"""Extension -> module name under reader.formats. Data only: `read.py hook` executes this file to tell a
document from anything else before it imports the reader."""
FORMATS = {
    "hwpx": "hwpx", "hwp": "hwp",
    "odt": "odf", "ods": "odf", "odp": "odf",
    "docm": "ooxml", "pptm": "ooxml", "ppsx": "ooxml",
    "docx": "ooxml", "dotx": "ooxml", "dotm": "ooxml",
    "pptx": "ooxml", "potx": "ooxml", "potm": "ooxml", "ppsm": "ooxml",
    "xlsx": "ooxml", "xlsm": "ooxml", "xltx": "ooxml", "xltm": "ooxml",
    "vsdx": "vsdx", "epub": "epub", "xps": "xps", "oxps": "xps",
    "doc": "doc", "xls": "xls", "ppt": "ppt", "xlsb": "xlsb",
    "rtf": "rtf", "wpd": "wpd", "wp": "wpd", "wp5": "wpd", "wp6": "wpd", "wp7": "wpd", "wpt": "wpd",
    "mobi": "mobi", "azw3": "mobi",
    "pages": "iwork", "numbers": "iwork", "key": "iwork",
    "djvu": "djvu", "djv": "djvu",
    "mdb": "jet", "accdb": "jet", "mde": "jet", "accde": "jet", "accdr": "jet",
    "pub": "pub", "vsd": "vsd", "vst": "vsd", "vsdm": "vsdx", "vstx": "vsdx", "mpp": "mpp", "mpt": "mpp",
    "pdf": "pdf",
    "wmf": "picture", "emf": "picture", "wmz": "picture", "emz": "picture", "tif": "picture", "tiff": "picture",
    "pct": "picture", "pict": "picture", "wpg": "picture",
}
