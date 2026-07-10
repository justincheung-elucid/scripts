#!/usr/bin/env python3

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pydicom
import pandas as pd
import yaml

from src.pydicom_utils import (
    tag_to_string,
    describe_name,
    index_elements,
    format_sequence_value,
    parse_tag_string,
    NameContext,
)
from src.pandas_utils import print_df_custom
from src.pseudotags import compute_pseudotags
from src.tqdmcustom import tqdm as tqdm

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("filepath_in", help="Path to a DICOM file, or a directory of DICOM files")
    parser.add_argument(
        "-n",
        "--hide-null",
        action="store_true",
        help="Hide tags that are entirely absent from the file (value is None)",
    )
    parser.add_argument(
        "-e",
        "--hide-empty",
        action="store_true",
        help="Hide tags that are present but have an empty string value",
    )
    parser.add_argument(
        "-f",
        "--filters",
        nargs="+",
        metavar="FILE_OR_TAG",
        help=(
            "One or more YAML files (each a list of tags), tag strings, or a mix of "
            "both, to restrict output to. Tag strings are robust to formatting: "
            "\"0020,000D\", \"(0020,000D)\", \"0x0020,0x000D\", \"( 0x0020 , 0x000D )\", "
            "etc. all work, and the same applies to entries within the YAML files. "
            "Names are still auto-resolved, same as unfiltered output. The printed "
            "tags are the union across everything given."
        ),
    )
    parser.add_argument(
        "-u",
        "--hide-uniform",
        action="store_true",
        help="Directory mode: hide tags whose value is the same across every file",
    )
    parser.add_argument(
        "-s",
        "--hide-scattered",
        action="store_true",
        help=(
            "Directory mode: hide tags where no two files share a value "
            "(every file's value is unique)"
        ),
    )
    parser.add_argument(
        "-p",
        "--pseudotags",
        action="store_true",
        help=(
            "Add rows for derived 'pseudotags' that split a single tag's compound "
            "value into more directly meaningful sub-fields (currently: Siemens "
            "ScanOptions -> gate/phase)"
        ),
    )
    parser.add_argument(
        "-1",
        "--one",
        action="store_true",
        help=(
            "When filepath_in is a directory, run on a single representative file from "
            "it (rather than aggregating over all of them) and print that file's path"
        ),
    )
    parser.add_argument(
        "-w",
        "--max-colwidth",
        type=int,
        default=100,
        help="Truncate printed cell values to this many characters (default: 100)",
    )
    parser.add_argument(
        "-c",
        "--compact",
        action="store_true",
        help=(
            "Skip pretty-printing: no box-drawn dividers between rows/columns, and "
            "dict-valued cells (e.g. directory-mode value counts) render as a flat "
            "repr instead of JSON-style indentation"
        ),
    )
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    path = Path(args.filepath_in)
    picked_file = None
    is_directory_aggregate = path.is_dir() and not args.one

    if path.is_dir():
        if args.one:
            picked_file = pick_representative_file(path)
            rows = build_rows(read_dataset(picked_file), args.filters, args.pseudotags)
        else:
            rows = build_rows_for_directory(path, args.filters, args.pseudotags)
    else:
        rows = build_rows(read_dataset(path), args.filters, args.pseudotags)

    if args.hide_null:
        rows = [row for row in rows if not row_is_all(row, None)]
    if args.hide_empty:
        rows = [row for row in rows if not row_is_all(row, "")]
    if args.hide_uniform:
        rows = [row for row in rows if not row_is_uniform(row)]
    if args.hide_scattered:
        rows = [row for row in rows if not row_is_scattered(row)]

    if not rows:
        print("No tags matched the given filters.", file=sys.stderr)
        sys.exit(1)

    if is_directory_aggregate:
        rows = [rename_value_column(row) for row in rows]

    df = pd.DataFrame(rows).set_index("tag")
    print_df_custom(df, max_colwidth=args.max_colwidth, pretty=not args.compact)

    if picked_file is not None:
        print(picked_file.resolve())

# ===== DETAILED IMPLEMENTATION =====================
def read_dataset(filepath: Path) -> pydicom.Dataset:
    return pydicom.dcmread(filepath, stop_before_pixels=True)

def list_directory_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.dcm") if p.is_file())

def pick_representative_file(directory: Path) -> Path:
    # Abstracted so the selection strategy can be swapped later. Currently: oldest
    # file by modification time.
    return min(list_directory_files(directory), key=lambda p: p.stat().st_mtime)

VALUE_COUNTS_COLUMN = "value counts (unique value -> file count)"

def rename_value_column(row: dict) -> dict:
    return {(VALUE_COUNTS_COLUMN if k == "value" else k): v for k, v in row.items()}

def row_is_all(row: dict, sentinel) -> bool:
    """True if every file's value for this row's tag equals `sentinel` -- generalizes
    the single-file "value is X" check to the {value: file_count} dicts produced by
    build_rows_for_directory."""
    value = row["value"]
    if isinstance(value, dict):
        return set(value) == {str(sentinel)}
    return value == sentinel

def row_is_uniform(row: dict) -> bool:
    """True if every file agrees on this tag's value. No-op outside directory mode."""
    value = row["value"]
    return isinstance(value, dict) and len(value) == 1

def row_is_scattered(row: dict) -> bool:
    """True if no two files share this tag's value. No-op outside directory mode."""
    value = row["value"]
    return isinstance(value, dict) and value and all(count == 1 for count in value.values())

def build_rows(
    ds: pydicom.Dataset, filter_paths: list[str] | None, pseudotags: bool = False
) -> list[dict]:
    context = NameContext(ds)
    if filter_paths:
        elements = index_elements(ds)
        rows = [build_row(context, tag, elements.get(tag)) for tag in load_filters(filter_paths)]
    else:
        rows = [build_row(context, elem.tag, elem) for elem in ds.iterall()]
    if pseudotags:
        rows += compute_pseudotags(ds)
    return rows

def build_row(
    context: NameContext, tag: pydicom.tag.BaseTag, elem: pydicom.DataElement | None
) -> dict:
    return {
        "tag": tag_to_string(tag),
        "name": describe_name(context, tag, elem),
        "VR": elem.VR if elem else None,
        "VM": elem.VM if elem else None,
        "value": (
            (format_sequence_value(elem.value) if elem.VR == "SQ" else elem.value)
            if elem
            else None
        ),
    }

def _build_rows_for_file(filepath: Path, filter_paths: list[str] | None, pseudotags: bool) -> list[dict]:
    return build_rows(read_dataset(filepath), filter_paths, pseudotags)

def build_rows_for_directory(
    directory: Path, filter_paths: list[str] | None, pseudotags: bool = False
) -> list[dict]:
    tag_order: list[str] = []
    first_seen_row: dict[str, dict] = {}
    value_counts_by_tag: dict[str, Counter] = {}

    files = list_directory_files(directory)
    # Each file's parsing is independent until this aggregation step, and is the
    # dominant cost for large directories, so it's worth spreading across cores.
    # Capped at half the available CPUs to leave headroom for other work on the
    # machine while this runs.
    max_workers = max(1, (os.cpu_count() or 1) // 2)
    print(f"Using {max_workers} process(es)", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        per_file_rows = executor.map(
            _build_rows_for_file, files, [filter_paths] * len(files), [pseudotags] * len(files)
        )
        for rows in tqdm(per_file_rows, total=len(files), desc="Reading files"):
            for row in rows:
                tag = row["tag"]
                if tag not in first_seen_row:
                    tag_order.append(tag)
                    first_seen_row[tag] = row
                    value_counts_by_tag[tag] = Counter()
                value_counts_by_tag[tag][str(row["value"])] += 1

    return [
        {**first_seen_row[tag], "value": dict(value_counts_by_tag[tag])}
        for tag in tag_order
    ]

def load_filters(entries: list[str]) -> list[pydicom.tag.BaseTag]:
    tags: list[pydicom.tag.BaseTag] = []
    seen: set[pydicom.tag.BaseTag] = set()

    def add(tag: pydicom.tag.BaseTag):
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    for entry in entries:
        tag = parse_tag_string(entry)
        if tag is not None:
            add(tag)
            continue
        # Not a recognizable tag string -- treat it as a YAML file path instead.
        with open(entry) as f:
            tag_strs = yaml.safe_load(f) or []
        for tag_str in tag_strs:
            tag = parse_tag_string(str(tag_str))
            if tag is None:
                raise ValueError(f"Could not parse tag {tag_str!r} in {entry}")
            add(tag)
    return tags

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())
