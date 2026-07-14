#!/usr/bin/env python3
"""Find a per-slice discriminating tag for a 4D (e.g. multiphasic) DICOM series,
following Cornerstone3D's splitImageIdsBy4DTags strategy: group images by spatial
position, sanity-check that the grouping is even (getIPPGroups), then try
candidate tags until one proves consistent (test4DTag). By default every tag
found in the series' first file is tried (in practice, a fixed candidate list
rarely contains the tag that actually validates) -- pass --tags to restrict the
search to specific tags instead, e.g. --tags taglists/multiphasic_candidates.yaml
for Cornerstone3D's own curated list.

If no candidate tag validates, that alone doesn't mean the series is single-phase:
a successful position grouping (N groups of M>1 images each) already proves M
distinct series are interleaved -- we just failed to find the tag that says which
image belongs to which. So a monotonically-increasing tag (see MONOTONIC_TAG
below) is always also checked, to at least rank each position group's images
consistently -- with arbitrary phase indices (0..M-1) rather than a real phase
percent, since assigning the *correct* phase to each rank is deferred.

Each series-level directory found anywhere under the given path(s) -- i.e. any
directory that directly contains *.dcm files -- is processed independently, so
pointing this at a higher-level export directory (e.g. one containing many
patient/series subdirectories) works the same as pointing it at one series.

Absorbs tag_combos.py's more verbose per-condition diagnostics (value-repeat
counts, unique-value products, distinct-count histograms, bad-group examples)
so every attempted tag's full check breakdown -- not just VALID/rejected -- is
available, without having to reach for a separate script."""

import argparse
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pydicom

from src.pydicom_utils import NameContext, describe_name, load_tag_list, parse_tag_string, tag_to_string

logger = logging.getLogger(__name__)

POSITION_TAG = "0020,0032"  # ImagePositionPatient

# InstanceNumber (IS -- integer string), not SOPInstanceUID, is the fallback
# monotonic tag: DICOM defines InstanceNumber specifically to express acquisition
# order within a series, and being numeric it admits a real strict-order
# comparison. SOPInstanceUID is *usually* assigned in generation order too, but
# it's a dotted-decimal UID, not a single integer -- comparing it numerically or
# lexicographically is unreliable across vendors (e.g. a trailing "...9" vs
# "...10" sorts wrong lexicographically), so it can't back a "strictly
# increasing" test the way InstanceNumber can.
MONOTONIC_TAG = "0020,0013"  # InstanceNumber

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        help=(
            "One or more paths -- shell globs work, e.g. multiphasic_separator.py "
            "~/data/SQA_*/. Each is searched (recursively) for series-level "
            "directories (ones directly containing *.dcm files), and each of those "
            "is processed independently."
        ),
    )
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
# Namespaced under a subdirectory (rather than outputs/ directly, like describe.py
# uses) so running this script and describe.py against the same folder don't
# clobber each other's file -- sanitize_path_for_filename only keys off the input
# path, not which tool produced the report.
OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs" / "multiphasic_separator"

def sanitize_path_for_filename(path: Path) -> str:
    resolved = str(path.resolve()).lstrip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", resolved)


# Which of the four outcome buckets a series' report gets filed under -- see
# categorize() below.
CATEGORY_NOT_MULTIPHASIC = "not_multiphasic"
CATEGORY_DISCRIMINATOR_TAG = "discriminator_tag"
CATEGORY_INSTANCE_NUMBER = "instance_number"
CATEGORY_UNSEPARABLE = "unseparable"

def output_path_for(path: Path, category: str) -> Path:
    return OUTPUTS_DIR / category / f"{sanitize_path_for_filename(path)}.txt"

def find_series_directories(path: Path) -> list[Path]:
    """Every directory anywhere under (or at) `path` that directly contains at
    least one *.dcm file -- each is treated as one series. Grouping by each
    file's immediate parent (rather than e.g. checking path.glob("*.dcm") at
    every level) naturally finds these regardless of how deeply nested they are,
    and reduces to just [path] itself for the common case of already pointing
    this at a single series directory."""
    return sorted({p.parent for p in path.rglob("*.dcm")})

@dataclass
class Check:
    label: str
    passed: bool
    detail: str

@dataclass
class Report:
    """Global, mutable scratch space for verdict strings that exist purely for
    human-readable reporting. Deliberately NOT threaded through function return
    values/parameters (that would be the "right" way to do it) -- kept as global
    state instead so that functions like group_series_by_position(), which are
    meant to mirror what a future C++ port would actually compute, don't have
    their signatures polluted with string-building concerns that only matter for
    this Python script's own reporting. Reset at the top of every
    separate_phases() call."""
    multiphasic_verdict: str | None = None

    def reset(self):
        self.multiphasic_verdict = None

report = Report()

def status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"

def parse_tag(s: str) -> pydicom.tag.BaseTag:
    tag = parse_tag_string(s)
    if tag is None:
        raise ValueError(f"Could not parse tag {s!r}")
    return tag

def tag_value(ds: pydicom.Dataset, tag: pydicom.tag.BaseTag) -> str | None:
    return str(ds[tag].value) if tag in ds else None

def numeric_tag_value(ds: pydicom.Dataset, tag: pydicom.tag.BaseTag) -> int | None:
    """None for a missing value *or* one that can't be read as an int -- both are
    equally disqualifying for a strict numeric-ordering test."""
    if tag not in ds:
        return None
    try:
        return int(ds[tag].value)
    except (TypeError, ValueError):
        return None

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

def group_series_by_position(
    datasets: list[pydicom.Dataset],
) -> defaultdict[str, list[pydicom.Dataset]] | None:
    """
    Analogous to Cornerstone3D's getIPPGroups;
    group by position and enforce structural constraints on positional groups.
    Returns the positional groups, or None if any check failed. The one-line
    multiphasic verdict for the SUMMARY is written to the global `report`
    instead of returned (see Report).

    If a check fails, then we should assume upstream that we return just everything in one series.
    """
    position_groups: defaultdict[str, list[pydicom.Dataset]] = defaultdict(list)
    for ds in datasets:
        position = tag_value(ds, parse_tag(POSITION_TAG))
        if position is None:
            logger.warning("Multiphase separation - a file is missing its position value. TODO - what file?")
            report.multiphasic_verdict = "INCONCLUSIVE (at least one file is missing a position value)"
            return None
        position_groups[position].append(ds)
    logger.debug("group_series_by_position: all %d files have a position value", len(datasets))

    # Check if the number of phases is the same across every position
    frame_counts = [len(v) for v in position_groups.values()]
    if len(set(frame_counts)) != 1:
        # Once we know sizes differ, log a breakdown for debugging
        size_histogram: dict[int, int] = defaultdict(int)
        for size in frame_counts:
            size_histogram[size] += 1
        logger.debug(
            "group_series_by_position: non-uniform position-group sizes -- "
            "{group size: number of positions with that size} = %s",
            dict(sorted(size_histogram.items())),
        )
        report.multiphasic_verdict = "INCONCLUSIVE (irregular position grouping)"
        return None
    logger.debug("group_series_by_position: all position groups are the same size (%d)", frame_counts[0])

    # Check if all positions are size 1 (only need to see one).
    if frame_counts[0] == 1:
        report.multiphasic_verdict = "NO (single image per position -- not multiphasic)"
        return None

    report.multiphasic_verdict = f"YES ({len(position_groups)} positions x {frame_counts[0]} phases)"
    return position_groups

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
    counts: dict[str | None, int] = defaultdict(int)
    for values in per_group_values.values():
        for v in values:
            counts[v] += 1

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

    value_sets: dict[frozenset, int] = defaultdict(int)
    for values in per_group_values.values():
        value_sets[frozenset(values)] += 1
    if len(value_sets) > 1:
        # Summarize by set *size* (weighted by how many groups have a set of that
        # size), rather than dumping one line per distinct set -- there can be as
        # many distinct sets as there are groups.
        size_histogram: dict[int, int] = defaultdict(int)
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

def monotonic_checks(
    datasets: list[pydicom.Dataset],
    groups: dict[str, list[pydicom.Dataset]],
    tag: pydicom.tag.BaseTag,
) -> tuple[list[Check], bool]:
    """Diagnostics for the monotonic-tag check (see module docstring). Unlike a
    discriminator tag, this tag isn't expected to repeat the same value set across
    position groups -- there's no "identical set across groups" check here. It only
    needs to (a) impose a real, strict order across the whole series, matching the
    filename order files were already read in, and (b) be unique within each
    position group, so that group's images can be ranked at all -- which rank means
    which phase is a separate, currently-unsolved problem (see module docstring)."""
    numeric_values = [numeric_tag_value(ds, tag) for ds in datasets]
    missing = sum(1 for v in numeric_values if v is None)
    if missing:
        check_increasing = Check(
            "Tag is strictly increasing across all files (filename order)",
            False,
            f"{missing}/{len(datasets)} file(s) have a missing or non-numeric value",
        )
    else:
        violation = next(
            (i for i in range(1, len(numeric_values)) if numeric_values[i] <= numeric_values[i - 1]),
            None,
        )
        check_increasing = Check(
            "Tag is strictly increasing across all files (filename order)",
            violation is None,
            ("strictly increasing across all files" if violation is None else
             f"file {violation}'s value ({numeric_values[violation]}) does not exceed "
             f"the previous file's value ({numeric_values[violation - 1]})"),
        )

    per_group_values = {pos: [tag_value(ds, tag) for ds in dslist] for pos, dslist in groups.items()}
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
        "Within each position group, tag values are all unique",
        not bad_groups,
        detail,
    )

    checks = [check_increasing, check_distinct_within_group]
    overall = check_increasing.passed and check_distinct_within_group.passed
    return checks, overall

# ===== REPORTING ====================================
CandidateResult = tuple[str, pydicom.tag.BaseTag, list[Check], bool]

def summarize(
    multiphasic_verdict: str,
    groups: dict[str, list[pydicom.Dataset]] | None,
    candidate_results: list[CandidateResult],
    monotonic_result: CandidateResult | None,
) -> list[str]:
    """The three top-of-file questions, answered up front so the bottom-line
    result doesn't require reading the full per-tag check breakdown below it."""
    lines = [f"1. Multiphasic (by IPP grouping): {multiphasic_verdict}"]

    if groups is None:
        lines.append("2. Separable by discriminating tag: N/A (not multiphasic, or inconclusive)")
        lines.append("3. Sortable by a monotonic tag: N/A (not multiphasic, or inconclusive)")
        return lines

    valid = [f"{name} ({tag_to_string(tag)})" for name, tag, _checks, overall in candidate_results if overall]
    lines.append(
        f"2. Separable by discriminating tag: {'YES -- ' + ', '.join(valid) if valid else 'NO'}"
    )

    mono_name, mono_tag, _checks, mono_overall = monotonic_result
    lines.append(
        f"3. Sortable by {mono_name} ({tag_to_string(mono_tag)}): {'YES' if mono_overall else 'NO'}"
    )
    return lines

def categorize(
    groups: dict[str, list[pydicom.Dataset]] | None,
    candidate_results: list[CandidateResult],
    monotonic_result: CandidateResult | None,
) -> str:
    """Which of the four outcome buckets this series' report should be filed
    under, mirroring the three SUMMARY questions in priority order: not
    multiphasic at all, else separable by a discriminating tag, else at least
    sortable by the monotonic tag, else outright unseparable."""
    if groups is None:
        return CATEGORY_NOT_MULTIPHASIC
    if any(overall for _name, _tag, _checks, overall in candidate_results):
        return CATEGORY_DISCRIMINATOR_TAG
    if monotonic_result is not None and monotonic_result[3]:
        return CATEGORY_INSTANCE_NUMBER
    return CATEGORY_UNSEPARABLE

def format_groups(groups: dict[str, list[pydicom.Dataset]]) -> list[str]:
    """The actual position groups formed -- which files ended up in which group --
    so a grouping that looks structurally fine (see position_checks) but is
    substantively wrong (e.g. the position tag doesn't mean what we assumed) can
    still be caught by inspection."""
    lines = []
    for idx, (position, dslist) in enumerate(groups.items(), start=1):
        lines.append(f"{idx}. Position {position!r}:")
        for ds in dslist:
            lines.append(f"     {Path(ds.filename).name}")
        lines.append("")
    return lines

def format_report(
    folder: Path,
    total_files: int,
    multiphasic_verdict: str,
    groups: dict[str, list[pydicom.Dataset]] | None,
    candidate_results: list[CandidateResult],
    monotonic_result: CandidateResult | None,
) -> str:
    lines = [
        "===== SUMMARY =====",
        *summarize(multiphasic_verdict, groups, candidate_results, monotonic_result),
        "",
        f"Folder: {folder.resolve()}",
        f"Position tag: {tag_to_string(parse_tag(POSITION_TAG))}",
        f"Total files: {total_files}",
        "",
    ]

    if groups is None:
        lines.append("Structural precondition failed -- no candidate tags were evaluated.")
        return "\n".join(lines) + "\n"

    group_size = len(next(iter(groups.values())))
    lines.append(f"Position groups: {len(groups)} groups of {group_size} images each.")
    lines.append("")
    lines.append("Position groups formed:")
    lines.append("")
    lines.extend(format_groups(groups))
    lines.append("Candidate tags tried, in order:")
    lines.append("")
    for idx, (name, tag, checks, overall) in enumerate(candidate_results, start=1):
        lines.append(f"{idx}. {name} ({tag_to_string(tag)}): {'VALID' if overall else 'rejected'}")
        for check in checks:
            lines.append(f"     {check.label}: {status(check.passed)}")
            lines.append(f"       {check.detail}")
        lines.append("")

    mono_name, mono_tag, mono_checks, mono_overall = monotonic_result
    lines.append(f"Monotonic tag: {mono_name} ({tag_to_string(mono_tag)}): {'VALID' if mono_overall else 'rejected'}")
    for check in mono_checks:
        lines.append(f"     {check.label}: {status(check.passed)}")
        lines.append(f"       {check.detail}")
    return "\n".join(lines) + "\n"

def separate_phases(
    input_folder: Path,
) -> list[Path]:
    ret = [input_folder] # always include the original series folder

    # Non-recursive: find_series_directories() already resolved `folder` down to
    # an exact directory that directly holds its own *.dcm files -- rglob here
    # would double-count any files under a nested series subdirectory instead of
    # leaving them for that subdirectory's own, independent run.
    files = sorted(input_folder.glob("*.dcm"))
    if not files:
        print(f"No DICOM files found directly under {input_folder}.", file=sys.stderr)
        return False
    datasets = [pydicom.dcmread(f, stop_before_pixels=True) for f in files]

    context = NameContext(datasets[0])
    # candidates = [(describe_name(context, tag), tag) for tag in load_tag_list("taglists/multiphasic_candidates.yaml")] # in case we want to try a different approach. Claude, don't port this.
    candidates = all_tags_in(context, datasets[0], exclude=parse_tag(POSITION_TAG))

    groups = group_series_by_position(datasets)

    candidate_results = []
    monotonic_result = None
    if groups is not None:
        for name, tag in candidates:
            checks, overall = candidate_checks(groups, tag, len(datasets))
            candidate_results.append((name, tag, checks, overall))

        monotonic_name = describe_name(context, parse_tag(MONOTONIC_TAG))
        mono_checks, mono_overall = monotonic_checks(datasets, groups, parse_tag(MONOTONIC_TAG))
        monotonic_result = (monotonic_name, parse_tag(MONOTONIC_TAG), mono_checks, mono_overall)

    report_text = format_report(
        input_folder, len(datasets), report.multiphasic_verdict,
        groups, candidate_results, monotonic_result,
    )
    category = categorize(groups, candidate_results, monotonic_result)
    output_file = output_path_for(input_folder, category)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(report_text)

    print(input_folder)
    for line in summarize(report.multiphasic_verdict, groups, candidate_results, monotonic_result):
        print(f"  {line}")
    print(f"  Wrote {output_file}")
    return True

def main():
    args = parse_args()
    for path_arg in args.paths:
        path = Path(path_arg)
        series_dirs = find_series_directories(path)
        if not series_dirs:
            print(f"WARNING: No DICOM files found under {path}.", file=sys.stderr)
            continue
        for series_dir in series_dirs:
            report.reset()
            ret = separate_phases(series_dir)

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main()
