"""Budgets and the one error type every reader module raises."""

MAX_INPUT = 512 * 2**20
ZIP_TOTAL = 2**30
ZIP_RATIO = 200
ZIP_RATIO_FLOOR = 2**20  # the ratio is checked only for members over 1 MiB
MAX_DEPTH = 64
MAX_OUTPUT = 20 * 10**6
CONVERT_SECONDS = 45
HOOK_SECONDS = 55     # the gate's whole budget, hashing included, under the 60-second hook timeout in hooks.json
HOOK_CHILD_MIN = 5    # with less than this left the gate denies instead of starting a conversion
SHEET_ROWS = 100_000
SHEET_COLS = 1_000        # columns one sheet row may reach
SHEET_CELLS = 500_000     # cells one document's sheets may materialize
SHEET_CHARS = 20_000_000  # characters of cell text, copies counted, one document's sheets may materialize
# XML elements one xlsx workbook's sheet parts may close, styled empty cells included, and the entries its shared-string
# table may hold. ooxml_sheets caps the other streamed parts: the shared strings at SST_ELEMENTS (2) times this many
# closed elements, the styles at STYLE_ELEMENTS (1,000,000). Measured: 2,000,000 cells stream in 1.4 s, the whole xlsx
# reader takes 3.2 s over them, and 7.1 s over the worst case all three caps allow together (test_reader_xlsx measure)
SHEET_SCAN = 2_000_000
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
BIN_RECORDS = 2_000_000    # records of one BIFF stream, one BIFF12 part or one PowerPoint Document stream
DECRYPT_BYTES = 32 * 2**20  # bytes one xls or ppt stream may decrypt
EMBED_BYTES = 16 * 2**20    # bytes of one Excel workbook stream embedded in an hwp or doc object
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
SMARTART_NODES = 5_000  # SmartArt text items one document may emit, copies counted; one data part reads at most four times as many points and connections
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
