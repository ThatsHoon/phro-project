"""pdfnative: a standard-library pdf parser, ported from dochan.

Port of the pdf parser of dochan 1.21.0 (commit 29bb52be2cf40750e0dab25b24d87e33d0fcedd7,
MIT; see THIRD_PARTY_NOTICES.md). Only the Python path is ported: dochan's C accelerators
and their backend switches are dropped. Caps keep dochan's names; every cap that drops
content is counted by the caller.

Module map (a module appears once its task has landed):
- objects.py: lexer and object syntax, indirect objects, object streams
- structure.py: xref tables and streams, trailer, page tree, recovery by scan
- filters.py: stream decoders (Flate, LZW, RunLength, ASCIIHex, ASCII85, predictors)
- crypto.py: the standard security handler
- cmap.py, cid_unicode.py, widths.py, core14.py, fonts.py: fonts and text decoding
- content.py, show_table.py, ocg.py: content stream interpreter
- annotations.py, images.py, document.py: annotations, page images and page assembly
"""
