from collections import Counter

import pydicom

from src.pydicom_utils import parse_tag_string, tag_to_string

def combo_real_tags(combo_groups: list[list[str]]) -> list[pydicom.tag.BaseTag]:
    """Real (non-pseudotag) DICOM tags referenced by any --combos group, so they can
    be unioned into the filtered tag set even when --filters didn't ask for them --
    otherwise a combo referencing an unfiltered tag would silently see nothing but
    None for it."""
    tags: list[pydicom.tag.BaseTag] = []
    seen: set[pydicom.tag.BaseTag] = set()
    for group in combo_groups:
        for raw in group:
            tag = parse_tag_string(raw)
            if tag is not None and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return tags

def combo_tag_label(raw: str) -> str:
    """Resolve a --combos argument to the same string used in the "tag" column:
    the canonical "(GGGG,EEEE)" form for real tags (however the user spelled it),
    or the literal string as-is for pseudotags (e.g. "ScanOptionsGate")."""
    tag = parse_tag_string(raw)
    return tag_to_string(tag) if tag is not None else raw

def compute_combo_rows(
    combo_groups: list[list[str]], file_values_by_tag: dict[str, dict[object, object]]
) -> list[dict]:
    """One row per --combos group: value is {str(value_tuple): file_count} -- the
    same {value: count} shape build_rows_for_directory already produces for
    ordinary tags, so combo rows flow through the existing rendering/-n/-e/-u/-s
    machinery with no special-casing.

    `file_values_by_tag` must map each combo tag's *resolved* label (see
    combo_tag_label) to {file_key: raw_value} -- i.e. the per-file values that the
    normal per-tag Counter aggregation throws away. Building that mapping is the
    caller's job (see build_rows_for_directory/build_rows in describe.py) since it
    differs between single-file and multi-file directory aggregation."""
    rows = []
    for group in combo_groups:
        labels = [combo_tag_label(raw) for raw in group]
        file_keys: set = set()
        for label in labels:
            file_keys |= set(file_values_by_tag.get(label, {}))
        counts = Counter(
            str(tuple(file_values_by_tag.get(label, {}).get(key) for label in labels))
            for key in file_keys
        )
        rows.append(
            {
                "tag": f"COMBO({','.join(labels)})",
                "name": "[Combo] " + " x ".join(labels),
                "VR": None,
                "VM": None,
                "value": dict(counts),
            }
        )
    return rows
