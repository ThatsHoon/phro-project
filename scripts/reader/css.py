"""The small CSS cascade the document reader needs: visibility (display:none, visibility hidden, collapse or
visible) and bold, nothing else."""
import re

MAX_SHEET = 2**20     # characters of one stylesheet that are read
MAX_SELECTORS = 5000  # simple selectors one Css keeps
MAX_BRACES = 200_000  # braces one stylesheet is scanned for
MAX_RULES = 20_000    # rules one stylesheet may yield

_PROPS = frozenset({"display", "visibility", "font-weight"})
_SEL = re.compile(r"^([A-Za-z][A-Za-z0-9:\-]*)?(?:\.([\w\-]+))?$")


def _strip_comments(s: str) -> str:
    out, i = [], 0
    while True:
        j = s.find("/*", i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:j])
        k = s.find("*/", j + 2)
        if k < 0:
            return "".join(out)
        i = k + 2


_BRACE = re.compile(r"[{}]")


def _rules(s: str, stats: dict):
    """(prelude, body) of each top-level style rule in one pass; at-rules and what is nested in them are
    skipped. Stops at MAX_BRACES braces or MAX_RULES rules and sets stats["capped"]."""
    depth = start = body = seen = rules = 0
    prelude, skipping = "", False
    for m in _BRACE.finditer(s):
        seen += 1
        if seen > MAX_BRACES:
            stats["capped"] = True
            return
        if m.group() == "{":
            if depth == 0:
                prelude = s[start:m.start()].rsplit(";", 1)[-1].strip()
                skipping, body = prelude.startswith("@"), m.end()
            depth += 1
        elif depth == 0:
            start = m.end()
        else:
            depth -= 1
            if depth == 0:
                start = m.end()
                if not skipping:
                    rules += 1
                    if rules > MAX_RULES:
                        stats["capped"] = True
                        return
                    yield prelude, s[body:m.start()]


def _decls(body: str) -> dict:
    """{display|visibility|font-weight: (value, important)} of one declaration block; the last one wins unless
    !important."""
    out = {}
    for d in body[:10_000].split(";"):
        name, sep, val = d.partition(":")
        name = name.strip().lower()
        if not sep or name not in _PROPS:
            continue
        val, imp = val.strip().lower(), False
        if "!important" in val:
            val, imp = val.replace("!important", "").strip(), True
        if name not in out or imp or not out[name][1]:
            out[name] = (val, imp)
    return out


class Css:
    """Selectors tag, .class, tag.class and comma lists for display, visibility and font-weight; the rest is ignored."""

    def __init__(self, sheets: list[str]):
        self._idx: dict = {}  # (tag, class) -> {prop: (important, order, value)}
        self.capped = False   # a size or count limit dropped part of the sheets
        stats = {}
        n = order = 0
        for sheet in sheets:
            self.capped |= len(sheet) > MAX_SHEET
            for prelude, body in _rules(_strip_comments(sheet[:MAX_SHEET]), stats):
                props = _decls(body)
                if not props:
                    continue
                order += 1
                for sel in prelude.split(","):
                    m = _SEL.match(sel.strip())
                    if not m or not (m.group(1) or m.group(2)):
                        continue
                    n += 1
                    if n > MAX_SELECTORS:
                        self.capped = True
                        return
                    slot = self._idx.setdefault(((m.group(1) or "").lower(), m.group(2) or ""), {})
                    for name, (val, imp) in props.items():
                        if name not in slot or (imp, order) >= slot[name][:2]:
                            slot[name] = (imp, order, val)
            self.capped |= bool(stats)

    def _props(self, node) -> dict:
        best = {}  # prop -> (rank, value)

        def put(slot, spec):
            for name, (imp, order, val) in slot.items():
                rank = (imp, 0, spec, order)
                if name not in best or rank > best[name][0]:
                    best[name] = (rank, val)
        if self._idx:
            tag = node.tag
            put(self._idx.get((tag, ""), {}), (0, 1))
            for c in node.attrs.get("class", "").split()[:32]:
                put(self._idx.get(("", c), {}), (1, 0))
                put(self._idx.get((tag, c), {}), (1, 1))
        for name, (val, imp) in _decls(node.attrs.get("style", "")).items():
            rank = (imp, 1, (2, 2), 0)
            if name not in best or rank > best[name][0]:
                best[name] = (rank, val)
        return {k: v[1] for k, v in best.items()}

    def visibility(self, node) -> str:
        """How node's own rules show it: "none" (display:none: it and all inside it are gone, nothing inside can
        show), "hidden" (visibility hidden or collapse, which its descendants inherit until one says visible),
        "visible", or "" (it says nothing and inherits)."""
        props = self._props(node)
        if props.get("display") == "none":
            return "none"
        v = props.get("visibility")
        return "hidden" if v in ("hidden", "collapse") else "visible" if v == "visible" else ""

    def bold(self, node) -> bool:
        v = self._props(node).get("font-weight", "")
        return v in ("bold", "bolder") or (v.isdigit() and int(v) >= 600)
