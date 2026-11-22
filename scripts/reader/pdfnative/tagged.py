"""The logical structure of a tagged pdf (ISO 32000-1 14.7, 14.8): the structure elements that
own each page's marked content, their standard types and their order in the tree.

A file is tagged when its catalog's /MarkInfo /Marked is true and its /StructTreeRoot has a
/ParentTree. A page is read on demand (`Tags.page`): its /StructParents keys the parent tree
(a number tree, 14.7.4.4), whose value lists, by marked-content id, the element that owns it;
each element and its ancestors (through /P) are read once for the document. An element gets its
type (mapped through /RoleMap to a standard type, at most ROLE_STEPS steps; an unknown type
stays as written and groups like a Div), its /Alt and /ActualText and a cell's /RowSpan and
/ColSpan. A marked-content id's place in the tree is the path of kid indexes from the root to
it, the last its place in its element's /K: the page's ids are ranked by it (the walk sequence
of a depth-first reading). The parent tree lookup visits at most TREE_NODES nodes, an element
chain STRUCT_DEPTH levels, a page PAGE_IDS ids; what lies past a cap is in no element, and the
layout reads it. A PDF 2.0 /Artifact element owns nothing.

`Tags.block` is the element that a marked-content id's text is a block of: the nearest
ancestor of a block type (BLOCKS), unless a cell, a list item's label or body, a caption, a
note, a table-of-contents entry or a figure (ABSORB) holds it before a list or a table does;
then that holder, so a cell or a list body that holds paragraphs is one block; a list item
holds what lies in it outside a label and a body. An id with no block ancestor is in no block
(-1). Element ids are object numbers, or numbers from DIRECT up for elements written inline.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .annotations import text_string
from .objects import PDFName, PDFRef
from .structure import PDFFile

TREE_NODES = 10_000      # parent tree nodes one lookup visits
STRUCT_DEPTH = 100       # elements from a marked-content id up to the root
PAGE_IDS = 100_000       # marked-content ids read for one page
ROW_CELLS = 10_000       # cells read in one table row
ROLE_STEPS = 16          # /RoleMap steps followed for one type
TEXT_CHARS = 10_000      # chars of one /Alt or /ActualText kept
DIRECT = 1 << 40

HEADINGS = {"H": 1, "H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}
STANDARD = frozenset((
    "Document", "DocumentFragment", "Part", "Art", "Sect", "Div", "Aside", "BlockQuote", "Caption",
    "TOC", "TOCI", "Index", "NonStruct", "Private", "P", "Title", "FENote", "Sub", "L", "LI", "Lbl",
    "LBody", "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot", "Span", "Quote", "Note",
    "Reference", "BibEntry", "Code", "Link", "Annot", "Ruby", "RB", "RT", "RP", "Warichu", "WT",
    "WP", "Em", "Strong", "Figure", "Formula", "Form", "Artifact", *HEADINGS))
BLOCKS = frozenset(("P", "Title", "Caption", "TOCI", "Index", "BibEntry", "Note", "FENote",
                    "BlockQuote", "Code", "Formula", "Figure", "Form", "Aside", "Lbl", "LBody", "LI",
                    "TD", "TH", *HEADINGS))
ABSORB = frozenset(("TD", "TH", "Lbl", "LBody", "Caption", "Note", "FENote", "TOCI", "Figure"))
BARRIERS = frozenset(("L", "Table", "TR", "THead", "TBody", "TFoot"))


@dataclass
class Element:
    kind: str                 # the standard type, or the type as written when /RoleMap gives none
    parent: int               # id of the parent element, -1 for a kid of the root
    alt: str = ""
    actual: Optional[str] = None
    span: Tuple[int, int] = (1, 1)   # (/RowSpan, /ColSpan) of a table cell's /A attributes
    node: dict = field(default_factory=dict, repr=False)
    path: Optional[tuple] = None     # kid indexes from the root, once known
    places: Optional[dict] = None    # {kid id or ("mcid", n): its index in /K}, once read


class Tags:
    """The structure tree of a tagged file, read page by page."""

    def __init__(self, pdf: PDFFile, root: dict, tree):
        self.pdf, self.root, self.tree = pdf, root, tree
        self.roles = pdf.resolve(root.get("RoleMap"))
        self.elements: Dict[int, Element] = {}
        self._direct: Dict[int, int] = {}
        self._pages: Dict[int, Dict[int, Tuple[int, int]]] = {}
        self._root_places: Optional[dict] = None

    # -- elements

    def _id(self, ref, node) -> int:
        if isinstance(ref, PDFRef):
            return ref.num
        return self._direct.setdefault(id(node), DIRECT + len(self._direct))

    def _load(self, ref) -> int:
        """The id of the element `ref` points at, read with its ancestors (through /P up to the
        root); -1 when it is no element, is in an /Artifact one, or its chain passes STRUCT_DEPTH
        or loops."""
        chain, seen, parent = [], set(), -1
        while True:
            node = self.pdf.resolve(ref)
            if not isinstance(node, dict) or node is self.root or _named(self.pdf, node, "Type") == "StructTreeRoot":
                break
            key = self._id(ref, node)
            if key in self.elements:
                parent = key
                break
            if key in seen or len(chain) >= STRUCT_DEPTH:
                return -1
            seen.add(key)
            chain.append((key, node))
            ref = node.get("P")
        for key, node in reversed(chain):
            self.elements[key] = self._element(node, parent)
            parent = key
        return -1 if any(self.elements[i].kind == "Artifact" for i in self.ancestors(parent)) else parent

    def _element(self, node: dict, parent: int) -> Element:
        pdf = self.pdf
        el = Element(self._kind(_named(pdf, node, "S")), parent, node=node)
        alt, actual = pdf.resolve(node.get("Alt")), pdf.resolve(node.get("ActualText"))
        el.alt = " ".join(text_string(alt, TEXT_CHARS).split()) if isinstance(alt, bytes) else ""
        el.actual = text_string(actual, TEXT_CHARS) if isinstance(actual, bytes) else None
        if el.kind in ("TD", "TH"):
            el.span = self._span(node.get("A"))
        return el

    def _kind(self, kind: str) -> str:
        for _ in range(ROLE_STEPS):
            if kind in STANDARD:
                return kind
            mapped = self.pdf.resolve(self.roles.get(kind)) if isinstance(self.roles, dict) else None
            if not isinstance(mapped, PDFName) or str(mapped) == kind:
                break
            kind = str(mapped)
        return kind

    def _span(self, attrs) -> Tuple[int, int]:
        """(/RowSpan, /ColSpan) of an /A attribute object or an array of them (14.8.5.7)."""
        attrs = self.pdf.resolve(attrs)
        rows, cols = 1, 1
        for a in (attrs if isinstance(attrs, list) else [attrs])[:16]:
            a = self.pdf.resolve(a)
            if not isinstance(a, dict):
                continue
            for key in ("RowSpan", "ColSpan"):
                v = self.pdf.resolve(a.get(key))
                if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1_000:
                    rows, cols = (v, cols) if key == "RowSpan" else (rows, v)
        return rows, cols

    def _places(self, kids) -> dict:
        """{kid element id, or ("mcid", n) for a marked-content id: its index} of a /K value."""
        out = {}
        for j, kid in enumerate(self._list(kids)[:PAGE_IDS]):
            node = self.pdf.resolve(kid)
            if isinstance(node, int) and not isinstance(node, bool):
                out.setdefault(("mcid", node), j)
            elif isinstance(node, dict):
                mcid = self.pdf.resolve(node.get("MCID"))
                if _named(self.pdf, node, "Type") == "MCR":
                    if isinstance(mcid, int) and not isinstance(mcid, bool):
                        out.setdefault(("mcid", mcid), j)
                else:
                    out.setdefault(self._id(kid, node), j)
        return out

    def _path(self, key: int) -> tuple:
        """The kid indexes from the root to element `key` (a missing place sorts last)."""
        chain = list(self.ancestors(key))
        for k in range(len(chain) - 1, -1, -1):
            el = self.elements[chain[k]]
            if el.path is not None:
                continue
            if k == len(chain) - 1:
                if self._root_places is None:
                    self._root_places = self._places(self.root.get("K"))
                at, before = self._root_places.get(chain[k], PAGE_IDS), ()
            else:
                parent = self.elements[chain[k + 1]]
                if parent.places is None:
                    parent.places = self._places(parent.node.get("K"))
                at, before = parent.places.get(chain[k], PAGE_IDS), parent.path
            el.path = before + (at,)
        return self.elements[key].path

    # -- pages

    def page(self, index: int) -> Dict[int, Tuple[int, int]]:
        """{marked-content id: (rank in tree order, owning element)} of a 0-based page."""
        known = self._pages.get(index)
        if known is not None:
            return known
        pdf = self.pdf
        page = pdf.pages()[index][0] if index < len(pdf.pages()) else {}
        number = pdf.resolve(page.get("StructParents"))
        owners = self._lookup(number) if isinstance(number, int) and not isinstance(number, bool) else None
        owners = pdf.resolve(owners)
        found = []
        for mcid, ref in enumerate(owners[:PAGE_IDS] if isinstance(owners, list) else ()):
            key = self._load(ref) if ref is not None else -1
            if key < 0:
                continue
            el = self.elements[key]
            if el.places is None:
                el.places = self._places(el.node.get("K"))
            found.append((self._path(key) + (el.places.get(("mcid", mcid), PAGE_IDS),), mcid, key))
        found.sort()
        self._pages[index] = out = {mcid: (rank, key) for rank, (_path, mcid, key) in enumerate(found)}
        return out

    def _lookup(self, number: int):
        """The parent tree's value for `number`: a number tree walk (7.9.7) down the /Kids whose
        /Limits hold it, at most TREE_NODES nodes."""
        pdf = self.pdf
        stack, seen = [self.tree], 0
        while stack and seen < TREE_NODES:
            node = pdf.resolve(stack.pop())
            seen += 1
            if not isinstance(node, dict):
                continue
            nums = pdf.resolve(node.get("Nums"))
            if isinstance(nums, list):
                for i in range(0, len(nums) - 1, 2):
                    if pdf.resolve(nums[i]) == number:
                        return nums[i + 1]
            kids = pdf.resolve(node.get("Kids"))
            for kid in reversed(kids[:TREE_NODES] if isinstance(kids, list) else ()):
                below = pdf.resolve(kid)
                limits = pdf.resolve(below.get("Limits")) if isinstance(below, dict) else None
                ends = [pdf.resolve(v) for v in limits] if isinstance(limits, list) and len(limits) == 2 else []
                if ends and all(isinstance(v, int) for v in ends) and not ends[0] <= number <= ends[1]:
                    continue
                stack.append(kid)
        return None

    # -- queries

    def ancestors(self, i: int):
        """Element `i` and its ancestors, nearest first."""
        seen = 0
        while i in self.elements and seen <= STRUCT_DEPTH:
            yield i
            i = self.elements[i].parent
            seen += 1

    def block(self, element: int) -> int:
        """The block that an element's content is part of (module doc), or -1."""
        chain = list(self.ancestors(element))
        nearest = next((k for k, i in enumerate(chain) if self.elements[i].kind in BLOCKS), None)
        if nearest is None:
            return -1
        found = chain[nearest]
        for i in chain[nearest:]:
            kind = self.elements[i].kind
            if kind in BARRIERS:
                break
            if kind in ABSORB or (kind == "LI" and self.elements[found].kind not in ("Lbl", "LBody")):
                found = i
        return found

    def nearest(self, element: int, kinds) -> int:
        """The nearest of `element` and its ancestors whose type is in `kinds`, or -1."""
        return next((i for i in self.ancestors(element) if self.elements[i].kind in kinds), -1)

    def depth(self, element: int, kind: str) -> int:
        """How many of `element` and its ancestors have type `kind`."""
        return sum(1 for i in self.ancestors(element) if self.elements[i].kind == kind)

    def rows(self, table: int, cells) -> List[List[int]]:
        """The rows of a /Table element that hold any of `cells` (a table may run over pages, and
        a page shows the rows its cells are in): the TR of each such cell, in tree order, as the
        TH and TD kids of that TR in /K order, at most ROW_CELLS of them."""
        trs = {}
        for c in cells:
            tr = self.nearest(c, ("TR",))
            if tr >= 0 and tr not in trs and self.nearest(tr, ("Table",)) == table:
                trs[tr] = self._path(tr)
        return [[k for k in self._kids(tr) if self.elements[k].kind in ("TH", "TD")] for tr in sorted(trs, key=trs.get)]

    def _list(self, kids) -> list:
        """A /K value as a list of its kids, each as written (a reference kept a reference)."""
        got = self.pdf.resolve(kids)
        return got if isinstance(got, list) else [kids]

    def _kids(self, i: int) -> List[int]:
        """The element kids of element `i`, in /K order, read as needed."""
        out = []
        for kid in self._list(self.elements[i].node.get("K"))[:ROW_CELLS]:
            node = self.pdf.resolve(kid)
            if not isinstance(node, dict) or "S" not in node:
                continue
            key = self._id(kid, node)
            if key not in self.elements:
                self.elements[key] = self._element(node, i)
            out.append(key)
        return out


def _named(pdf: PDFFile, node: dict, key: str) -> str:
    """The name at `key` of a dictionary, or ""."""
    name = pdf.resolve(node.get(key))
    return str(name) if isinstance(name, PDFName) else ""


def read(pdf: PDFFile) -> Optional[Tags]:
    """The structure tree of a tagged file, or None for a file that is not tagged."""
    mark = pdf.resolve(pdf.catalog.get("MarkInfo"))
    root = pdf.resolve(pdf.catalog.get("StructTreeRoot"))
    if not (isinstance(mark, dict) and pdf.resolve(mark.get("Marked")) is True and isinstance(root, dict)):
        return None
    tree = root.get("ParentTree")
    return Tags(pdf, root, tree) if isinstance(pdf.resolve(tree), dict) else None
