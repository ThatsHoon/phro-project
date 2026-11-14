"""pdf optional content: which layers are off in the default configuration.

ISO 32000-1 8.11. `OCState.from_catalog` reads /OCProperties /D (/BaseState, /ON, /OFF).
`hidden` takes an optional content group (OCG) or membership dictionary (OCMD), or a
reference to one. An OCMD follows its /P policy: AnyOn (the default), AllOn, AnyOff or AllOff.
A /VE visibility expression is not evaluated and counts as visible. Usage-application
dictionaries (/AS) are not read: /D alone decides.

A group is told apart by the identity of its resolved dictionary: the file resolves each
object once, so a reference and the dictionary it names are the same object.
"""
from .structure import PDFFile


class OCState:
    def __init__(self, pdf: PDFFile, off_ids=frozenset(), on_ids=frozenset(), base_off=False,
                 keep=()):
        self._pdf = pdf
        self._off = off_ids
        self._on = on_ids
        self._base_off = base_off
        self._keep = keep  # holds the group dictionaries, so their ids stay theirs

    @classmethod
    def from_catalog(cls, pdf: PDFFile) -> "OCState":
        properties = pdf.resolve(pdf.catalog.get("OCProperties"))
        config = pdf.resolve(properties.get("D")) if isinstance(properties, dict) else None
        if not isinstance(config, dict):
            return cls(pdf)
        keep = []

        def ids(key):
            listed = pdf.resolve(config.get(key))
            found = set()
            for item in listed if isinstance(listed, list) else []:
                group = pdf.resolve(item)
                if isinstance(group, dict):
                    keep.append(group)
                    found.add(id(group))
            return frozenset(found)

        base = pdf.resolve(config.get("BaseState"))
        return cls(pdf, ids("OFF"), ids("ON"), str(base or "") == "OFF", tuple(keep))

    def _group_off(self, group: dict) -> bool:
        gid = id(group)
        if self._base_off:
            return gid not in self._on
        return gid in self._off

    def hidden(self, props) -> bool:
        """True when the group or membership dictionary `props` is off."""
        props = self._pdf.resolve(props)
        if not isinstance(props, dict):
            return False
        if str(self._pdf.resolve(props.get("Type")) or "") != "OCMD":
            return self._group_off(props)
        if "VE" in props:
            return False
        members = self._pdf.resolve(props.get("OCGs"))
        if isinstance(members, dict):
            members = [members]
        if not isinstance(members, list):
            return False
        states = []  # True where a group is off
        for item in members:
            group = self._pdf.resolve(item)
            if isinstance(group, dict):
                states.append(self._group_off(group))
        if not states:
            return False
        policy = str(self._pdf.resolve(props.get("P")) or "AnyOn")
        if policy == "AllOn":
            return any(states)
        if policy == "AnyOff":
            return not any(states)
        if policy == "AllOff":
            return not all(states)
        return all(states)  # AnyOn

    def any_off(self, resources) -> bool:
        """True when the page's /Properties, or the /OC of an XObject it draws, names an off
        group."""
        if not isinstance(resources, dict):
            return False
        properties = self._pdf.resolve(resources.get("Properties"))
        for value in properties.values() if isinstance(properties, dict) else ():
            if self.hidden(value):
                return True
        xobjects = self._pdf.resolve(resources.get("XObject"))
        for value in xobjects.values() if isinstance(xobjects, dict) else ():
            stream = self._pdf.resolve(value)
            dictionary = getattr(stream, "dictionary", None)
            if isinstance(dictionary, dict) and "OC" in dictionary and self.hidden(dictionary["OC"]):
                return True
        return False
