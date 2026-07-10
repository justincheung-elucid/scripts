#!/usr/bin/env python3

import argparse

import pydicom
import pandas as pd
import yaml

from src.pydicom_utils import tag_to_string, describe_name, find_element
from src.pandas_utils import print_df_custom

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("filepath_in", help="Path to DICOM file")
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
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    ds = pydicom.dcmread(args.filepath_in, stop_before_pixels=True)
    rows = build_rows(ds, args.filters)

    if args.hide_null:
        rows = [row for row in rows if row["value"] is not None]
    if args.hide_empty:
        rows = [row for row in rows if row["value"] != ""]

    df = pd.DataFrame(rows).set_index("tag")
    print_df_custom(df)

# ===== DETAILED IMPLEMENTATION =====================
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
            (elem.value if elem.VR != "SQ" else f"<Sequence, {len(elem.value)} item(s)>")
            if elem
            else None
        ),
    }

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
