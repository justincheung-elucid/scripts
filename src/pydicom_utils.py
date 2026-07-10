import re
from functools import lru_cache
from pathlib import Path

import pydicom
from pydicom.datadict import dictionary_description

# dicom3tools (David Clunie's private DICOM tag dictionaries, vendored as a git
# submodule) is the source of truth for private tag names here -- it's far more
# complete than pydicom's own bundled private dictionary. See
# deps/3p/dicom3tools/libsrc/standard/elmdict/*.tpl, one file per vendor.
_ELMDICT_DIR = (
    Path(__file__).resolve().parent.parent
    / "deps" / "3p" / "dicom3tools" / "libsrc" / "standard" / "elmdict"
)

_TPL_LINE_RE = re.compile(
    r'^\((?P<group>[0-9A-Fa-f]{4}),(?P<elem>[0-9A-Fa-f]{4})\)'
    r'.*?VR="(?P<vr>[^"]*)"'
    r'.*?VM="(?P<vm>[^"]*)"'
    r'.*?Owner="(?P<owner>[^"]*)"'
    r'.*?Keyword="(?P<keyword>[^"]*)"'
    r'.*?Name="(?P<name>[^"]*)"'
)

# Manufacturer (matched as an uppercased substring of the file's (0008,0070)
# Manufacturer element) -> the dicom3tools template file covering that vendor's
# private tags. Add more vendors here as needed (see deps/3p/dicom3tools/
# libsrc/standard/elmdict/ for the full list: siemens.tpl, philips.tpl, etc.).
MANUFACTURER_TPL_FILES = {
    "GE MEDICAL": "gems.tpl",
}

# GE has kept these group -> private-creator assignments fixed for CT since the
# 1990s. Confirmed against the GEHC Discovery and Revolution CT DICOM Conformance
# Statements, Appendix B.1. Used as a best-guess fallback ONLY when a file's own
# (group,0010) Private Creator element is blank (e.g. stripped by de-identification)
# -- dicom3tools shows several of these groups are reused by other GE product lines
# (PET, ultrasound, mammo) under different creators, so this table is CT-specific
# and could mislabel tags on other modalities.
GE_LEGACY_PRIVATE_CREATORS = {
    0x0009: "GEMS_IDEN_01",
    0x0019: "GEMS_ACQU_01",
    0x0021: "GEMS_RELA_01",
    0x0023: "GEMS_STDY_01",
    0x0027: "GEMS_IMAG_01",
    0x0039: "GEMS_0039",
    0x0043: "GEMS_PARM_01",
    0x0045: "GEMS_HELIOS_01",
    0x0049: "GEMS_CT_CARDIAC_001",
    0x0053: "GEHC_CT_ADVAPP_001",
}

def tag_to_string(tag: pydicom.tag.BaseTag) -> str:
    return f"({tag.group:04X},{tag.element:04X})"

# Accepts e.g. "(0020,0032)", "(0x0020,0x0032)", "0020,0032", "0x0020,0x0032", and
# any of those with extra spaces around the parens/comma/hex components. Requires
# parens to be paired -- either both present or neither, not one without the other.
_PAREN_TAG_RE = re.compile(
    r'^\(\s*(?:0[xX])?([0-9A-Fa-f]{1,4})\s*,\s*(?:0[xX])?([0-9A-Fa-f]{1,4})\s*\)$'
)
_BARE_TAG_RE = re.compile(
    r'^(?:0[xX])?([0-9A-Fa-f]{1,4})\s*,\s*(?:0[xX])?([0-9A-Fa-f]{1,4})$'
)

def parse_tag_string(s: str) -> pydicom.tag.BaseTag | None:
    match = _PAREN_TAG_RE.match(s.strip()) or _BARE_TAG_RE.match(s.strip())
    if match is None:
        return None
    group_hex, element_hex = match.groups()
    return pydicom.tag.Tag(int(group_hex, 16), int(element_hex, 16))

def index_elements(ds: pydicom.Dataset) -> dict[pydicom.tag.BaseTag, pydicom.DataElement]:
    """Single-pass tag -> element lookup, including elements nested inside sequence
    items (e.g. GE's CT Cardiac Sequence fields under (0049,1001)). Building this
    once and reusing it for multiple tag lookups is far cheaper than re-scanning
    the whole dataset with ds.iterall() per tag."""
    index: dict[pydicom.tag.BaseTag, pydicom.DataElement] = {}
    for elem in ds.iterall():
        index.setdefault(elem.tag, elem)
    return index

def format_sequence_value(seq: pydicom.Sequence, max_preview: int = 3) -> str:
    summary = f"<Sequence, {len(seq)} item(s)>"
    if not seq:
        return summary
    first_item = list(seq[0])
    parts = []
    for e in first_item[:max_preview]:
        name = e.keyword or tag_to_string(e.tag)
        value = f"<Sequence, {len(e.value)} item(s)>" if e.VR == "SQ" else e.value
        parts.append(f"{name}={value}")
    suffix = ", ..." if len(first_item) > max_preview else ""
    return f"{summary} [{', '.join(parts)}{suffix}]"

@lru_cache(maxsize=None)
def _load_tpl(filename: str) -> dict[tuple[str, str, str], tuple[str, str, str]]:
    # dicom3tools stores private elements with the private-block byte zeroed out
    # (e.g. "0053,0020" covers the real tag regardless of which block 0x10-0xFF
    # ends up holding it), matching pydicom's own "ggggxxee"-style private-dictionary
    # convention. Keyed by (owner, group, offset), all lowercase hex strings.
    entries: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    with open(_ELMDICT_DIR / filename, encoding="latin-1") as f:
        for line in f:
            m = _TPL_LINE_RE.match(line)
            if not m:
                continue
            key = (m["owner"], m["group"].lower(), m["elem"].lower())
            entries[key] = (m["vr"], m["keyword"], m["name"])
    return entries

def _dicom3tools_name(tpl_file: str, creator: str, tag: pydicom.tag.BaseTag) -> str | None:
    offset = f"00{tag.element & 0xFF:02x}"
    entry = _load_tpl(tpl_file).get((creator, f"{tag.group:04x}", offset))
    if entry is None:
        return None
    _vr, _keyword, name = entry
    return name if name not in ("", "?") else None

def _actual_private_creator(ds: pydicom.Dataset, group: int) -> str | None:
    creator_tag = pydicom.tag.Tag(group, 0x0010)
    return ds[creator_tag].value if creator_tag in ds else None

def _manufacturer_tpl_file(ds: pydicom.Dataset) -> str | None:
    manufacturer = ds.get("Manufacturer", "").upper()
    for key, filename in MANUFACTURER_TPL_FILES.items():
        if key in manufacturer:
            return filename
    return None

def _tag_only_name(tag: pydicom.tag.BaseTag) -> str:
    # Mirrors what pydicom's own elem.name falls back to, but works from a bare
    # tag -- needed to resolve names for tags that aren't actually present in ds
    # (e.g. when driven by a --filters tag list rather than iterating the file).
    if tag.is_private_creator:
        return "Private Creator"
    try:
        return dictionary_description(tag)
    except KeyError:
        return "Private tag data" if tag.is_private else "Unknown"

class NameContext:
    """Per-dataset cache for the parts of name resolution that only depend on the
    dataset as a whole (its manufacturer, each private group's actual creator) --
    not the element being named. Building one of these once per file, instead of
    re-deriving this from `ds` on every describe_name() call, avoids a full
    pydicom.Dataset.__getitem__("Manufacturer") lookup per element, which dominates
    runtime on large directories (profiled: ~half of total time)."""

    def __init__(self, ds: pydicom.Dataset):
        self.tpl_file = _manufacturer_tpl_file(ds)
        self._ds = ds
        self._creator_by_group: dict[int, str | None] = {}

    def actual_private_creator(self, group: int) -> str | None:
        if group not in self._creator_by_group:
            self._creator_by_group[group] = _actual_private_creator(self._ds, group)
        return self._creator_by_group[group]

def describe_name(
    context: NameContext, tag: pydicom.tag.BaseTag, elem: pydicom.DataElement | None = None
) -> str:
    tpl_file = context.tpl_file
    fallback = _tag_only_name(tag)

    if tag.is_private_creator:
        # The creator element itself; when blanked (e.g. by de-identification) or
        # absent, show what we'd otherwise assume it to be so it's clear what's
        # guessed.
        guess = tpl_file and GE_LEGACY_PRIVATE_CREATORS.get(tag.group)
        actual_value = elem.value if elem is not None else None
        if not actual_value and guess:
            return f"{fallback} [{guess}?]"
        return fallback
    if not tag.is_private:
        return fallback

    if tpl_file is None:
        return fallback

    creator = context.actual_private_creator(tag.group)
    guessed = not creator
    if guessed:
        creator = GE_LEGACY_PRIVATE_CREATORS.get(tag.group)
    if not creator:
        return fallback

    name = _dicom3tools_name(tpl_file, creator, tag)
    if name is None:
        return fallback
    # Marked with a trailing "?" since this creator is assumed from GE's legacy
    # group convention, not read from the file's own (blanked) Private Creator
    # element -- unlike dicom3tools names resolved from a real declared creator.
    return f"[{name}?]" if guessed else f"[{name}]"
