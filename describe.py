#!/usr/bin/env python3

import argparse
from collections import Counter
from pathlib import Path

import pydicom
import pandas as pd
import yaml

from src.pydicom_utils import tag_to_string, describe_name, find_element, format_sequence_value
from src.pandas_utils import print_df_custom

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
        metavar="FILE",
        help=(
            "One or more YAML files mapping names to tags (e.g. MyTag: \"0020,000D\") "
            "to restrict output to. The printed tags are the union across all given files."
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
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    path = Path(args.filepath_in)
    picked_file = None

    if path.is_dir():
        if args.one:
            picked_file = pick_representative_file(path)
            rows = build_rows(read_dataset(picked_file), args.filters)
        else:
            rows = build_rows_for_directory(path, args.filters)
    else:
        rows = build_rows(read_dataset(path), args.filters)

    if args.hide_null:
        rows = [row for row in rows if not row_is_all(row, None)]
    if args.hide_empty:
        rows = [row for row in rows if not row_is_all(row, "")]

    df = pd.DataFrame(rows).set_index("tag")
    print_df_custom(df)

    if picked_file is not None:
        print(picked_file.resolve())

# ===== DETAILED IMPLEMENTATION =====================
def read_dataset(filepath: Path) -> pydicom.Dataset:
    return pydicom.dcmread(filepath, stop_before_pixels=True)

def list_directory_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file())

def pick_representative_file(directory: Path) -> Path:
    # Abstracted so the selection strategy can be swapped later. Currently: oldest
    # file by modification time.
    return min(list_directory_files(directory), key=lambda p: p.stat().st_mtime)

def row_is_all(row: dict, sentinel) -> bool:
    """True if every file's value for this row's tag equals `sentinel` -- generalizes
    the single-file "value is X" check to the {value: file_count} dicts produced by
    build_rows_for_directory."""
    value = row["value"]
    if isinstance(value, dict):
        return set(value) == {str(sentinel)}
    return value == sentinel

def build_rows(ds: pydicom.Dataset, filter_paths: list[str] | None) -> list[dict]:
    if filter_paths:
        return [
            build_row(tag, name, find_element(ds, tag))
            for tag, name in load_filters(filter_paths).items()
        ]
    return [build_row(elem.tag, describe_name(ds, elem), elem) for elem in ds.iterall()]

def build_row(tag: pydicom.tag.BaseTag, name: str, elem: pydicom.DataElement | None) -> dict:
    return {
        "tag": tag_to_string(tag),
        "name": name,
        "VR": elem.VR if elem else None,
        "VM": elem.VM if elem else None,
        "value": (
            (format_sequence_value(elem.value) if elem.VR == "SQ" else elem.value)
            if elem
            else None
        ),
    }

def build_rows_for_directory(directory: Path, filter_paths: list[str] | None) -> list[dict]:
    tag_order: list[str] = []
    first_seen_row: dict[str, dict] = {}
    value_counts_by_tag: dict[str, Counter] = {}

    for filepath in list_directory_files(directory):
        for row in build_rows(read_dataset(filepath), filter_paths):
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

def load_filters(paths: list[str]) -> dict[pydicom.tag.BaseTag, str]:
    tag_names: dict[pydicom.tag.BaseTag, str] = {}
    for path in paths:
        with open(path) as f:
            filter_config = yaml.safe_load(f) or {}
        for name, tag_str in filter_config.items():
            group, element = tag_str.split(",")
            tag_names[pydicom.tag.Tag(int(group, 16), int(element, 16))] = name
    return tag_names

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())
