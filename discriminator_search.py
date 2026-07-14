#!/usr/bin/env python3
"""Find a per-slice discriminating tag for a 4D (e.g. multiphasic) DICOM series,
following Cornerstone3D's splitImageIdsBy4DTags strategy: group images by spatial
position, sanity-check that the grouping is even (getIPPGroups), then try a
priority-ordered list of candidate tags until one proves consistent (test4DTag).

Absorbs tag_combos.py's more verbose per-condition diagnostics (value-repeat
counts, unique-value products, distinct-count histograms, bad-group examples)
so every attempted tag's full check breakdown -- not just VALID/rejected -- is
available, without having to reach for a separate script."""

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pydicom

from src.pydicom_utils import NameContext, describe_name, parse_tag_string, tag_to_string

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

# ===== CLI =========================================
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
        "--all-tags", "-a", action="store_true",
        help="Ignore --candidates/the default list and try every top-level tag found in the "
             "first DICOM file instead (assumes that file's tags are representative of the set)",
    )
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
# Namespaced under a subdirectory (rather than outputs/ directly, like describe.py
# uses) so running this script and describe.py against the same folder don't
# clobber each other's file -- sanitize_path_for_filename only keys off the input
# path, not which tool produced the report.
OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs" / "discriminator_search"

def sanitize_path_for_filename(path: Path) -> str:
    resolved = str(path.resolve()).lstrip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", resolved)

def output_path_for(path: Path) -> Path:
    return OUTPUTS_DIR / f"{sanitize_path_for_filename(path)}.txt"

@dataclass
class Check:
    label: str
    passed: bool
    detail: str

def status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"

def parse_tag(s: str) -> pydicom.tag.BaseTag:
    tag = parse_tag_string(s)
    if tag is None:
        raise ValueError(f"Could not parse tag {s!r}")
    return tag

def tag_value(ds: pydicom.Dataset, tag: pydicom.tag.BaseTag) -> str | None:
    return str(ds[tag].value) if tag in ds else None

def all_tags_in(
    context: NameContext, ds: pydicom.Dataset, exclude: pydicom.tag.BaseTag
) -> list[tuple[str, pydicom.tag.BaseTag]]:
    """(name, tag) for every top-level, non-sequence tag in `ds` except `exclude` --
    names go through describe_name so private tags (e.g. GE) get their
    dicom3tools-backed names instead of falling back to the bare tag string."""
    return [
        (describe_name(context, elem.tag, elem), elem.tag)
        for elem in ds
        if elem.tag != exclude and elem.VR != "SQ"
    ]

def position_checks(
    datasets: list[pydicom.Dataset], position_tag: pydicom.tag.BaseTag
) -> tuple[list[Check], dict[str, list[pydicom.Dataset]] | None]:
    """Structural preconditions on the position tag alone (Cornerstone3D's
    getIPPGroups), with tag_combos.py-style verbose detail on each. Returns the
    checks plus the resulting position groups, or None for groups if any check
    failed (grouping is refused rather than run on ill-formed data)."""
    groups = defaultdict(list)
    missing = 0
    for ds in datasets:
        position = tag_value(ds, position_tag)
        if position is None:
            missing += 1
        else:
            groups[position].append(ds)

    checks = [Check(
        "(getIPPGroups) Every file has a position value",
        missing == 0,
        f"{missing} file(s) missing a position value" if missing else "all files have a position value",
    )]
    if missing:
        return checks, None

    sizes = [len(v) for v in groups.values()]
    min_size = min(sizes)
    singles = sorted(pos for pos, v in groups.items() if len(v) <= 1)
    checks.append(Check(
        "Every position value repeats (count > 1)",
        min_size > 1,
        f"min count = {min_size}" + (f"; non-repeating value(s): {singles}" if singles else ""),
    ))

    distinct_sizes = sorted(set(sizes))
    checks.append(Check(
        "All position value counts are equal to each other",
        len(distinct_sizes) == 1,
        f"distinct counts seen: {distinct_sizes}",
    ))

    ok = all(check.passed for check in checks)
    return checks, (dict(groups) if ok else None)

def candidate_checks(
    groups: dict[str, list[pydicom.Dataset]],
    tag: pydicom.tag.BaseTag,
    total_files: int,
) -> tuple[list[Check], bool]:
    """Verbose per-candidate diagnostics, ported from tag_combos.py's print_summary
    (repeat counts, unique-value products, distinct-count histograms, bad-group
    examples). Overall pass/fail is still exactly Cornerstone3D's test4DTag (the
    last two checks) -- the rest are informative, not gating."""
    per_group_values = {pos: [tag_value(ds, tag) for ds in dslist] for pos, dslist in groups.items()}
    counts = Counter(v for values in per_group_values.values() for v in values)

    min_count = min(counts.values())
    singles = sorted(v for v, c in counts.items() if c <= 1)
    check_repeats = Check(
        "Every candidate value repeats (count > 1)",
        min_count > 1,
        f"min count = {min_count}" + (f"; non-repeating value(s): {singles}" if singles else ""),
    )

    product = len(groups) * len(counts)
    check_product = Check(
        "unique(position) x unique(candidate) == total files",
        product == total_files,
        f"{len(groups)} x {len(counts)} = {product}, total files = {total_files}",
    )

    distinct_counts = sorted(set(counts.values()))
    check_equal_counts = Check(
        "All candidate value counts are equal to each other",
        len(distinct_counts) == 1,
        f"distinct counts seen: {distinct_counts}",
    )

    bad_groups = [pos for pos, values in per_group_values.items() if len(values) != len(set(values))]
    if bad_groups:
        example = bad_groups[0]
        example_values = per_group_values[example]
        dupes = sorted({v for v in example_values if example_values.count(v) > 1})
        detail = (f"{len(bad_groups)}/{len(groups)} group(s) have a repeated value "
                  f"(e.g. position={example!r} repeats {dupes})")
    else:
        detail = f"checked {len(groups)} group(s), no repeats found"
    check_distinct_within_group = Check(
        "(test4DTag) Within each position group, candidate values are all distinct",
        not bad_groups,
        detail,
    )

    value_sets = Counter(frozenset(values) for values in per_group_values.values())
    if len(value_sets) > 1:
        # Summarize by set *size* (weighted by how many groups have a set of that
        # size), rather than dumping one line per distinct set -- there can be as
        # many distinct sets as there are groups.
        size_histogram = Counter()
        for value_set, n in value_sets.items():
            size_histogram[len(value_set)] += n
        detail = (f"{len(value_sets)} distinct value-sets found across {len(groups)} groups; "
                  f"group count by set size: {dict(sorted(size_histogram.items()))}")
    else:
        [(only_set, _)] = value_sets.items()
        detail = f"all {len(groups)} groups share one set of {len(only_set)} values"
    check_consistent_sets = Check(
        "(test4DTag) The set of candidate values is identical across every position group",
        len(value_sets) <= 1,
        detail,
    )

    checks = [
        check_repeats, check_product, check_equal_counts,
        check_distinct_within_group, check_consistent_sets,
    ]
    overall = check_distinct_within_group.passed and check_consistent_sets.passed
    return checks, overall

# ===== REPORTING ====================================
def format_report(
    folder: Path,
    position_tag: pydicom.tag.BaseTag,
    total_files: int,
    position_check_results: list[Check],
    groups: dict[str, list[pydicom.Dataset]] | None,
    candidate_results: list[tuple[str, pydicom.tag.BaseTag, list[Check], bool]],
) -> str:
    lines = [
        f"Folder: {folder.resolve()}",
        f"Position tag: {tag_to_string(position_tag)}",
        f"Total files: {total_files}",
        "",
        "Structural preconditions (position tag alone):",
    ]
    for i, check in enumerate(position_check_results, start=1):
        lines.append(f"  {i}. {check.label}: {status(check.passed)}")
        lines.append(f"     {check.detail}")
    lines.append("")

    if groups is None:
        lines.append("Structural precondition failed -- no candidate tags were evaluated.")
        lines.append("")
        lines.append("Valid discriminating tag(s): NONE")
        return "\n".join(lines) + "\n"

    group_size = len(next(iter(groups.values())))
    lines.append(f"Position groups: {len(groups)} groups of {group_size} images each.")
    lines.append("")
    lines.append("Candidate tags tried, in order:")
    lines.append("")
    for idx, (name, tag, checks, overall) in enumerate(candidate_results, start=1):
        lines.append(f"{idx}. {name} ({tag_to_string(tag)}): {'VALID' if overall else 'rejected'}")
        for check in checks:
            lines.append(f"     {check.label}: {status(check.passed)}")
            lines.append(f"       {check.detail}")
        lines.append("")

    valid = [f"{name} ({tag_to_string(tag)})" for name, tag, _checks, overall in candidate_results if overall]
    lines.append(f"Valid discriminating tag(s): {', '.join(valid) if valid else 'NONE'}")
    return "\n".join(lines) + "\n"

def main():
    args = parse_args()
    position_tag = parse_tag(args.position_tag)
    folder = Path(args.folder)

    files = sorted(folder.rglob("*.dcm"))
    if not files:
        print(f"No DICOM files found under {folder}.", file=sys.stderr)
        sys.exit(1)
    datasets = [pydicom.dcmread(f, stop_before_pixels=True) for f in files]

    context = NameContext(datasets[0])
    if args.all_tags:
        candidates = all_tags_in(context, datasets[0], exclude=position_tag)
    elif args.candidates:
        tags = [parse_tag(t) for t in args.candidates]
        candidates = [(describe_name(context, tag), tag) for tag in tags]
    else:
        candidates = [(name, parse_tag(tag_str)) for name, tag_str in CANDIDATES]

    position_check_results, groups = position_checks(datasets, position_tag)

    candidate_results = []
    if groups is not None:
        for name, tag in candidates:
            checks, overall = candidate_checks(groups, tag, len(datasets))
            candidate_results.append((name, tag, checks, overall))

    report = format_report(
        folder, position_tag, len(datasets), position_check_results, groups, candidate_results
    )
    output_file = output_path_for(folder)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report)

    valid_tags = [(name, tag) for name, tag, _checks, overall in candidate_results if overall]
    if valid_tags:
        for name, tag in valid_tags:
            print(f"{name} ({tag_to_string(tag)})")
    else:
        print("No valid discriminating tag found.")
    print(f"Wrote {output_file}")

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main()
