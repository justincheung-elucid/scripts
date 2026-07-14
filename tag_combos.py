#!/usr/bin/env python3
"""Print per-tag value counts and joint combo counts for two DICOM tags across
all files in a folder."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import pydicom

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="Path to a folder of DICOM files")
    parser.add_argument("tag1", help="First tag, e.g. 0018,0022")
    parser.add_argument("tag2", help="Second tag, e.g. 0020,0032")
    return parser.parse_args()

def parse_tag(s: str) -> pydicom.tag.BaseTag:
    group, element = s.strip().strip("()").split(",")
    return pydicom.tag.Tag(int(group, 16), int(element, 16))

def tag_value(ds: pydicom.Dataset, tag: pydicom.tag.BaseTag) -> str:
    return str(ds[tag].value) if tag in ds else None

def print_counts(label: str, counts: Counter):
    print(f"{label} ({len(counts)} unique values):")
    for value, count in counts.most_common():
        print(f"  {value}: {count}")
    print()

def status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"

def print_summary(tag1_label: str, tag2_label: str, counts1: Counter, counts2: Counter,
                   groups: dict, total_files: int):
    # groups: tag1 value -> list of tag2 values from files with that tag1 value.
    print("Summary:")

    min1 = min(counts1.values())
    singles1 = sorted(v for v, c in counts1.items() if c <= 1)
    print(f"  1. Every {tag1_label} value repeats (count > 1): {status(min1 > 1)}")
    print(f"     min count = {min1}"
          + (f"; non-repeating value(s): {singles1}" if singles1 else ""))

    min2 = min(counts2.values())
    singles2 = sorted(v for v, c in counts2.items() if c <= 1)
    print(f"  2. Every {tag2_label} value repeats (count > 1): {status(min2 > 1)}")
    print(f"     min count = {min2}"
          + (f"; non-repeating value(s): {singles2}" if singles2 else ""))

    product = len(counts1) * len(counts2)
    print(f"  3. unique({tag1_label}) x unique({tag2_label}) == total files: "
          f"{status(product == total_files)}")
    print(f"     {len(counts1)} x {len(counts2)} = {product}, total files = {total_files}")

    distinct_counts1 = sorted(set(counts1.values()))
    print(f"  4. All {tag1_label} value counts are equal to each other: "
          f"{status(len(distinct_counts1) == 1)}")
    print(f"     distinct counts seen: {distinct_counts1}")

    distinct_counts2 = sorted(set(counts2.values()))
    print(f"  5. All {tag2_label} value counts are equal to each other: "
          f"{status(len(distinct_counts2) == 1)}")
    print(f"     distinct counts seen: {distinct_counts2}")

    print(f"  6. Structural checks (tag1={tag1_label} as position, tag2={tag2_label} as discriminator):")

    missing1 = counts1.get(None, 0)
    print(f"     6.1 (getIPPGroups) Every file has a {tag1_label} value: {status(missing1 == 0)}")
    print(f"         {missing1} file(s) missing {tag1_label}")

    bad_groups = [v1 for v1, values in groups.items() if len(values) != len(set(values))]
    print(f"     6.2 (test4DTag) Within each {tag1_label} group, {tag2_label} values are all "
          f"distinct: {status(not bad_groups)}")
    if bad_groups:
        example = bad_groups[0]
        example_values = groups[example]
        dupes = sorted({v for v in example_values if example_values.count(v) > 1})
        print(f"         {len(bad_groups)}/{len(groups)} group(s) have a repeated value "
              f"(e.g. {tag1_label}={example!r} repeats {dupes})")
    else:
        print(f"         checked {len(groups)} group(s), no repeats found")

    set_counts = Counter(frozenset(values) for values in groups.values())
    print(f"     6.3 (test4DTag) The set of {tag2_label} values is identical across every "
          f"{tag1_label} group: {status(len(set_counts) <= 1)}")
    if len(set_counts) > 1:
        # Summarize by set *size* (weighted by how many groups have a set of that size),
        # rather than dumping one line per distinct set -- there can be as many distinct
        # sets as there are groups (e.g. when tag2 is collinear with tag1).
        size_histogram = Counter()
        for vset, n in set_counts.items():
            size_histogram[len(vset)] += n
        print(f"         {len(set_counts)} distinct value-sets found across {len(groups)} groups; "
              f"group count by set size: {dict(sorted(size_histogram.items()))}")
    else:
        [(only_set, _)] = set_counts.items()
        print(f"         all {len(groups)} groups share one set of {len(only_set)} values")

def main():
    args = parse_args()
    tag1 = parse_tag(args.tag1)
    tag2 = parse_tag(args.tag2)

    counts1 = Counter()
    counts2 = Counter()
    combo_counts = Counter()
    groups = defaultdict(list)
    files = sorted(Path(args.folder).rglob("*.dcm"))
    for path in files:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        value1 = tag_value(ds, tag1)
        value2 = tag_value(ds, tag2)
        counts1[value1] += 1
        counts2[value2] += 1
        combo_counts[(value1, value2)] += 1
        groups[value1].append(value2)

    print(f"Total files: {len(files)}\n")
    print_counts(f"Combos ({args.tag1}, {args.tag2})", combo_counts)
    print_counts(args.tag1, counts1)
    print_counts(args.tag2, counts2)
    print_summary(args.tag1, args.tag2, counts1, counts2, groups, len(files))

if __name__ == "__main__":
    main()
