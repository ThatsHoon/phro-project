"""Budgets and the one error type every reader module raises."""
import os

MAX_INPUT = 512 * 2**20
ZIP_TOTAL = 2**30
ZIP_RATIO = 200
ZIP_RATIO_FLOOR = 2**20  # the ratio is checked only for members over 1 MiB
MAX_DEPTH = 64
TEX_DEPTH = 32
MAX_OUTPUT = 20 * 10**6
CONVERT_SECONDS = 45
HOOK_SECONDS = 55     # the gate's whole budget, hashing included, under the 60-second hook timeout in hooks.json
HOOK_CHILD_MIN = 5    # with less than this left the gate denies instead of starting a conversion
SHEET_ROWS = 100_000
SHEET_COLS = 1_000        # columns one sheet row may reach
SHEET_CELLS = 500_000     # cells one document's sheets may materialize
SHEET_CHARS = 20_000_000  # characters of cell text, copies counted, one document's sheets may materialize
SHEET_RUNS = 20           # hidden row or column runs a sheet table's caption names; the rest are counted
SHEET_ISLAND_STEPS = 8 * SHEET_CELLS  # cells (and columns) one sheet's island splits may visit, all levels together
FORMULA_GROUPS = 1_000    # cell ranges one sheet's formulas paragraph lists; the groups past them are counted
SHEET_COMMENTS = 10_000   # cell comments one workbook may turn into notes; the rest are cut, counted once
FACT_VALUES = 20          # values or ranges one entry of a sheet's fact lists; the rest are counted in the entry
FACT_ITEMS = 1_000        # entries one fact paragraph of a sheet lists; the rest are counted
FORMULA_COPY_CHARS = 4_000_000  # characters of formula text one document's sheets read again for repeated cells
#                                 (an ods cell or row written once and repeated); the copies past them are counted
BLANK_FORMULAS = 100_000  # formula cells with no value one document's sheets keep; the rest are counted
# XML elements the sheet parts of one document's xlsx workbooks may open, styled empty cells included, and the entries
# a shared-string table may hold. ooxml_sheets caps the other streamed parts: the shared strings at SST_ELEMENTS (2)
# times this many opened elements, the styles at STYLE_ELEMENTS (1,000,000). Measured: 2,000,000 cells stream in 1.4 s, the whole xlsx
# reader takes 3.2 s over them, and 7.1 s over the worst case all three caps allow together (test_reader_xlsx measure)
SHEET_SCAN = 2_000_000
RUNNING_PICTURE = 3       # sections a picture is placed in to be linked at the first one only
GRID_COLS = 1_000        # columns a span may reach in a rendered table
GRID_SLOTS = 2_000_000    # grid slots all spans of one rendered table may cover
CHART_CELLS = 100_000     # cells one chart table may hold, header row included (series x rows)
# chart and embedded sheet table cells one docx, doc, pptx, hwp, hwpx, xls, xlsb or xlsx document may emit, copies
# counted (each placement of a chart is charged); the iWork charts (Task 14 `chart_block`) are charged to it too
CHART_TOTAL = 200_000
CFB_STREAM = 256 * 2**20  # bytes of one compound-file stream
CFB_SECTORS = 1_000_000
CFB_ENTRIES = 131_072     # directory entries
CFB_DEPTH = 128           # storage nesting
CFB_CHAIN = 4_000_000     # steps of one sector chain
HWP_RECORDS = 1_000_000   # records of one HWP stream
BIN_RECORDS = 2_000_000    # records of one document's BIFF streams, one BIFF12 part or one PowerPoint Document stream
DECRYPT_BYTES = 32 * 2**20  # bytes one xls or ppt stream may decrypt
VBA_MODULES = 256           # VBA modules one document reads, all its projects together
VBA_BYTES = 2 * 2**20       # bytes one document's VBA projects decompress to or scan (dir streams and source)
EMBED_BYTES = 16 * 2**20    # bytes of one Excel workbook stream embedded in an hwp or doc object
EMBED_TOTAL = 16 * EMBED_BYTES  # bytes the packages embedded in one document inflate to, all together
JET_WORK_TOTAL = 32 * 2**20  # work one Access file may spend on its rows, in units of about 0.2 us (a text byte
#                               decoded; jet.py prices rows and cells in them), at most 10 a byte of the file and 1 MiB
JET_PAGE_CACHE = 8_192       # decoded pages one Access file with a Jet3/Jet4 db_key keeps (32 MiB at 4 KiB)
# image bytes one docx, doc, pptx, hwp, hwpx, ppt, xls, xlsb, xlsx, rtf, mobi, azw3, wpd (wp, wp5, wp6, wp7, wpt), pages,
# numbers, key, pub, vsd, vsdx, Access (attachment pictures) or pdf document may emit, copies counted (each placement
# of a picture is charged; a pdf's renders are included); a djvu file renders none
IMAGE_BYTES = 256 * 2**20
# (an xls also caps the picture placements it reads, all its sheets together: xls_drawing.MAX_PLACEMENTS)
# tables one doc, docx, hwp, hwpx, rtf or pdf document may open, nested ones counted (an rtf or ppt table's cells are what
# it charges). A .doc paragraph opens up to 63 with one
# 3-byte sprmPItap, and a deflated docx part or hwp section holds one in about a byte; each costs the model about
# 600 bytes. A real document has thousands at most (a thousand-page form with ten tables a page has ten thousand);
# ten times that is past any, and holds the tables to about 60 MB and a second or two of parse and render
TABLE_NODES = 100_000
FORM_BYTES = 16 * 2**20  # form field data one .doc reads, each place in its Data stream once; past it a form field
#                          shows no state and is counted
SHAPE_ROWS = 100_000
ALT_CHUNK_DEPTH = 4  # altChunk parts one docx reads inside one another (a Word package imported into one imported)
ALT_CHUNKS = 64  # altChunk parts one document reads, every level together
ALT_CHUNK_BYTES = 2**27  # bytes of the altChunk parts one document reads, every level together
PROPERTY_STREAM = 2**20  # bytes of a .doc SummaryInformation or DocumentSummaryInformation stream read; past it, none
XYCUT_WORK = 400_000  # boxes the reading orders of one document may sort, summed over their cuts (deck.Order): about a second; then (top, left)
TOC_WORK = 2_000_000  # nodes one book's table-of-contents labels are matched over (about a second); past it the rest
#                       of the entries make no heading and are counted
TOC_ITEMS = 10_000  # table-of-contents entries one epub (nav, else NCX) or mobi (NCX index) book reads for headings
SMARTART_NODES = 5_000  # SmartArt text items one document may emit, copies counted; one data part reads at most four times as many points and connections
# metafile pictures (WMF, EMF, PICT, WPG, TIFF): each cap is read at call time
FIGURE_CHARS = 20_000      # figure text per picture
FIGURE_EDGE = 1_568        # long side of a figure PNG upscaled for small text, in pixels
FIGURE_EDGE_TEXT = 1_024   # long side of a figure PNG with text (and of a TIFF page), never upscaled otherwise
FIGURE_EDGE_PLAIN = 768    # long side of a figure PNG with no text, never upscaled
FIGURE_TEXT_PX = 12        # smallest text height, in pixels at 96 dpi, under which a figure is upscaled (at most 2x)
FIGURE_RESERVE = 3         # seconds left before the deadline under which no further picture is rendered
FIGURE_WHOLE = 0.7         # a pdf figure box, or an OCR picture area, over this share of the page takes the whole page
METAFILE_RECORDS = 1_000_000  # records of one WMF, EMF, PICT or WPG
METAFILE_POINTS = 4_000_000   # points across all paths of one picture
METAFILE_OBJECTS = 65_535     # WMF and EMF object table slots
METAFILE_NESTING = 16         # metafiles nested inside EMF+ image objects
# bytes all the metafiles nested in one picture may hand to a parse together, or twice the picture's own size when
# that is more: a picture nested many levels deep is parsed level by level, each level's bytes counted
METAFILE_NESTED = 64 * 2**20
TIFF_PAGES = 100           # pages of a lone TIFF
FONT_FILES = 4_000         # font files indexed
FONT_BYTES = 64 * 2**20    # bytes read from one font file
# the warning of a document whose figures the deadline left undrawn: its result is not kept in the cache
FIGURES_LATE = "figures not rendered: time"
# OCR of scanned pages and raster pictures: each cap is read at call time. The doubt signals (OCR_JUNK, OCR_UNREAD,
# OCR_UNREAD_NOCONF, OCR_CONF, OCR_LOW), OCR_WORKERS, the rule and area bounds and the Tesseract page segmentation
# modes are calibrated on the OCR spike's 30 synthetic pages (6 layouts at 150, 200 and 300 dpi, skewed 2 degrees and
# noisy): every noisy page doubtful, the most clean pages trusted
OCR_DPI = 200              # resolution a pdf page is rendered at for OCR
OCR_MIN_EDGE = 64          # long side, in pixels, under which a picture is not read
OCR_MIN_WORDS = 2          # words under which a picture gets no text
OCR_WORKERS = min(4, os.cpu_count() or 1)  # pages recognized at once; 12 pages: 2.12, 1.34, 0.92 s a page at 1, 2, 4
OCR_RULE = 0.05            # shortest ruled-table rule, as a share of the page side it runs along
OCR_RULE_THICK = 1 / 40    # thickest rule, in inches (at least 2 px); spike tables' rules 2-5 px at 150-300 dpi, noisy
#                            included, all found; 1/60 loses them at 150 and 200 dpi; no text page gives a rule at 1/20
OCR_RULES_MAX = 1_000      # rules a page may have; past this it is texture (a photo, halftone) and gives none
OCR_AREA = 0.04            # share of the page the unread ink's box must cover to make a picture area; a drawing or
#                            light photo pasted on a spike page: 0.12-0.45; Windows clean pages: 0
OCR_AREA_DENSITY = 0.01    # share of the unread box's pixels that must be unread ink for a picture area; spike:
#                            Tesseract's scattered misses on clean Korean pages 0.000-0.005, a line drawing pasted on
#                            a page 0.018-0.020, photos 0.29-0.62, light photos 0.21-0.84, a logo 0.93-0.94
OCR_PAPER = 32             # levels under the paper's mean from which a pixel is not paper (the picture area's ink);
#                            spike: a light photo (gray 150-250 on paper 255) boxed at 0.12-0.49 of the page
OCR_LIGHT_CELLS = 64       # cells along the page's short side of the grid on which light (not dark) ink is found as
#                            regions; a cell is ~0.1 inch at 150-300 dpi, under a text line
OCR_FLAT = 16              # gray range (levels) up to which a page with no words is blank; spike: clean pages 228-255,
#                            noisy 155-188, a blank render 0
OCR_SKEW = 0.5             # degrees the lines of a TESS_PSM_KOR read may slope before the page is doubtful; spike:
#                            straight pages 0.00-0.09, text pages turned 2 degrees 1.84-1.94 (CER 0.28-0.66)
OCR_ANSWER_MARGIN = 2      # seconds before the session's stop by which the worker must have answered a job
OCR_LATIN = 0.9            # share of Latin letters from which Tesseract eng reads a Windows page again
OCR_JUNK = 0.03            # share of junk characters over which a page is doubtful and a picture gets no text; clean
#                            spike pages at most 0.003, noisy 0-0.033: the other signals carry the noisy pages
OCR_UNREAD = 0.07          # share of dark pixels outside the word boxes over which a page is doubtful; spike: clean text
#                            pages at most 0.055 (Tesseract), noisy Windows Korean pages 0.087-0.121
OCR_UNREAD_NOCONF = 0.015  # the same share for a page whose words carry no confidence (Windows OCR, which has none
#                            to doubt it by); spike: clean Windows pages at most 0.007, noisy 0.062-0.413 (the English
#                            one 0.062, CER 0.42); public scan pages Windows read 0.027-0.727 (CER 0.18-0.61)
OCR_CONF = 88              # mean word confidence under which a Tesseract page is doubtful; spike: clean 90.2-96.6,
#                            noisy 71.6-83.5
OCR_LOW = 0.08             # share of low-confidence words over which a Tesseract page is doubtful; spike: clean at most
#                            0.039, noisy 0.131-0.292
OCR_LOW_WORD = 60          # confidence under which a word counts as low
TESS_PSM_LATIN = 3         # Tesseract page segmentation mode for a Latin read; spike English CER in reading order
#                            0.015 (psm 4: 0.206, psm 6: 0.141)
TESS_PSM_KOR = 4           # Tesseract page segmentation mode for a kor+eng read; spike CER in reading order 0.299
#                            (psm 3: 0.475, psm 6: 0.335)
OCR_FRAME_JSON = 2**20     # header bytes of one OCR worker frame
OCR_QUEUE_BYTES = 64 * 2**20  # pixel bytes waiting for the worker past which submitting a page waits (until the stop)
# the warning of a document whose OCR the deadline left unfinished
OCR_LATE = "ocr not finished: time"
# one Read of out.md: Claude Code's Read returns 2,000 lines by default, refuses a result over about 25,000 tokens and
# a file over about 256 KB without offset and limit; a read past these gets a limit that fits
READ_LINES = 2_000
LINE_CHARS = 2_000         # a paragraph's line is broken to this (Read cuts a longer line)
READ_TOKENS = 20_000
READ_TOKEN_BYTES = 3       # the estimate: one token per 3 UTF-8 bytes of a line, its newline counted
READ_BYTES = 240_000
KINDS = ("unsupported", "encrypted", "drm", "damaged", "too_large", "timeout")
# a too_large detail starts with one of these and a colon, so the gate can name the limit that was hit
BIG_INPUT = "input"  # the file is over MAX_INPUT
BIG_PART = "part"    # one zip member passes ZIP_RATIO
BIG_TOTAL = "total"  # the members read so far pass ZIP_TOTAL


class ReadError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
