#!/usr/bin/env python3
"""Find a per-slice discriminating tag for a 4D (e.g. multiphasic) DICOM series,
following Cornerstone3D's splitImageIdsBy4DTags strategy: group images by spatial
position, sanity-check that the grouping is even (getIPPGroups), then try a
priority-ordered list of candidate tags until one proves consistent (test4DTag)."""

import argparse
from collections import defaultdict
from pathlib import Path

import pydicom

POSITION_TAG = "0020,0032"  # ImagePositionPatient

# Cornerstone3D's own candidate order: standard tags first, vendor-private last.
CANDIDATES = [
    ("TemporalPositionIdentifier", "0020,0100"),
    ("DiffusionBValue", "0018,9087"),
    ("TriggerTime", "0018,1060"),
    ("EchoTime", "0018,0081"),
    ("EchoNumbers", "0018,0086"),
    ("Philips private B-value", "2001,1003"),
    ("Siemens private B-value", "0019,100C"),
    ("GE private B-value", "0043,1039"),
    ("PET FrameReferenceTime", "0054,1300"),
]

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="Path to a folder of DICOM files")
    parser.add_argument(
        "--position-tag", default=POSITION_TAG, metavar="TAG",
        help=f"Tag to group images by spatial position (default: {POSITION_TAG}, ImagePositionPatient)",
    )
    parser.add_argument(
        "--candidates", nargs="+", metavar="TAG",
        help="Override the default Cornerstone3D-style candidate list with these tags, tried in order",
    )
    parser.add_argument(
        "--all-tags", action="store_true",
        help="Ignore --candidates/the default list and try every top-level tag found in the "
             "first DICOM file instead (assumes that file's tags are representative of the set)",
    )
    return parser.parse_args()

def parse_tag(s: str) -> pydicom.tag.BaseTag:
    group, element = s.strip().strip("()").split(",")
    return pydicom.tag.Tag(int(group, 16), int(element, 16))

def tag_to_string(tag: pydicom.tag.BaseTag) -> str:
    return f"{tag.group:04X},{tag.element:04X}"

def tag_value(ds: pydicom.Dataset, tag: pydicom.tag.BaseTag) -> str:
    return str(ds[tag].value) if tag in ds else None

def all_tags_in(ds: pydicom.Dataset, exclude: pydicom.tag.BaseTag) -> list[tuple[str, str]]:
    """(name, tag_str) for every top-level, non-sequence tag in `ds` except `exclude`."""
    return [
        (elem.keyword or tag_to_string(elem.tag), tag_to_string(elem.tag))
        for elem in ds
        if elem.tag != exclude and elem.VR != "SQ"
    ]

def get_ipp_groups(
    datasets: list[pydicom.Dataset], position_tag: pydicom.tag.BaseTag
) -> dict[str, list[pydicom.Dataset]] | None:
    """Group datasets by position; return None (refuse to split) if any dataset
    lacks a position, any group has only one image, or group sizes aren't equal."""
    groups = defaultdict(list)
    for ds in datasets:
        position = tag_value(ds, position_tag)
        if position is None:
            print("  A file is missing the position tag -- refusing to split.")
            return None
        groups[position].append(ds)

    sizes = {len(group) for group in groups.values()}
    if any(len(group) <= 1 for group in groups.values()):
        print("  At least one position has only a single image -- refusing to split.")
        return None
    if len(sizes) > 1:
        print(f"  Position groups have unequal sizes ({sorted(sizes)}) -- refusing to split.")
        return None
    return groups

def test_4d_tag(groups: dict[str, list[pydicom.Dataset]], tag: pydicom.tag.BaseTag) -> bool:
    """A candidate is valid if, within every position, its values are all distinct
    (one value per phase), and the *set* of values is identical across every position."""
    value_sets = []
    for group in groups.values():
        values = [tag_value(ds, tag) for ds in group]
        if len(values) != len(set(values)):
            return False
        value_sets.append(frozenset(values))
    return len(set(value_sets)) == 1

def main():
    args = parse_args()
    position_tag = parse_tag(args.position_tag)

    files = sorted(Path(args.folder).rglob("*.dcm"))
    datasets = [pydicom.dcmread(f, stop_before_pixels=True) for f in files]
    print(f"Found {len(datasets)} files.\n")

    if args.all_tags:
        candidates = all_tags_in(datasets[0], exclude=position_tag)
        print(f"Trying all {len(candidates)} top-level tags found in {files[0]}.\n")
    elif args.candidates:
        candidates = [(t, t) for t in args.candidates]
    else:
        candidates = CANDIDATES

    print("Checking structural precondition (getIPPGroups)...")
    groups = get_ipp_groups(datasets, position_tag)
    if groups is None:
        print("\nResult: series cannot be split (structural precondition failed).")
        return
    group_size = len(next(iter(groups.values())))
    print(f"  {len(datasets)} files form {len(groups)} position groups of {group_size} images each. OK.\n")

    print("Searching for a valid discriminating tag (test4DTag)...")
    for name, tag_str in candidates:
        tag = parse_tag(tag_str)
        ok = test_4d_tag(groups, tag)
        print(f"  {name} ({tag_str}): {'VALID' if ok else 'rejected'}")
        if ok:
            print(f"\nResult: {name} ({tag_str}) is a valid discriminating tag.")
            return

    print("\nResult: no candidate tag validated -- series should be treated as unsplit.")

if __name__ == "__main__":
    main()
