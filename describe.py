#!/usr/bin/env python3

import argparse
import io
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pydicom
import pandas as pd

from src.pydicom_utils import (
    tag_to_string,
    describe_name,
    index_elements,
    format_sequence_value,
    load_tag_list,
    NameContext,
)
from src.pandas_utils import print_df_custom
from src.pseudotags import compute_pseudotags
from src.combos import combo_real_tags, combo_tag_label, compute_combo_rows

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "filepath_in",
        nargs="+",
        help=(
            "One or more paths, each a DICOM file or a directory of DICOM files -- "
            "shell globs work, e.g. describe.py ~/data/some-export/*/. Each path is "
            "processed independently and its table saved to outputs/."
        ),
    )
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
    parser.add_argument(
        "-o",
        "--combos",
        action="append",
        nargs="+",
        metavar="TAG",
        help=(
            "2+ tags (real tags or pseudotag names, same flexible format as "
            "--filters) to find the unique combinations of values for, across "
            "files in directory mode. May be given multiple times for independent "
            "combo groups. If a group references a pseudotag name, -p must also "
            "be given or that slot will just be None."
        ),
    )
    args = parser.parse_args()
    for group in args.combos or []:
        if len(group) < 2:
            parser.error(f"--combos requires at least 2 tags per group, got: {group}")
    return args

# ===== CORE IMPLEMENTATION =========================
OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"

def sanitize_path_for_filename(path: Path) -> str:
    resolved = str(path.resolve()).lstrip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", resolved)

def output_path_for(path: Path) -> Path:
    return OUTPUTS_DIR / f"{sanitize_path_for_filename(path)}.txt"

def main(args: argparse.Namespace):
    any_succeeded = False
    for filepath_in in args.filepath_in:
        if process_path(Path(filepath_in), args):
            any_succeeded = True
    if not any_succeeded:
        sys.exit(1)

def process_path(path: Path, args: argparse.Namespace) -> bool:
    picked_file = None
    is_directory_aggregate = path.is_dir() and not args.one
    combo_groups = args.combos or []

    if path.is_dir():
        if args.one:
            files = list_directory_files(path)
            if not files:
                print(f"No DICOM files found under {path}.", file=sys.stderr)
                return False
            picked_file = pick_representative_file(files)
            rows = build_rows(read_dataset(picked_file), args.filters, args.pseudotags, combo_groups)
            rows = append_single_snapshot_combos(rows, combo_groups)
        else:
            rows = build_rows_for_directory(path, args.filters, args.pseudotags, combo_groups)
    else:
        rows = build_rows(read_dataset(path), args.filters, args.pseudotags, combo_groups)
        rows = append_single_snapshot_combos(rows, combo_groups)

    if args.hide_null:
        rows = [row for row in rows if not row_is_all(row, None)]
    if args.hide_empty:
        rows = [row for row in rows if not row_is_all(row, "")]
    if args.hide_uniform:
        rows = [row for row in rows if not row_is_uniform(row)]
    if args.hide_scattered:
        rows = [row for row in rows if not row_is_scattered(row)]

    if not rows:
        print(f"No tags matched the given filters for {path}.", file=sys.stderr)
        return False

    if is_directory_aggregate:
        rows = [rename_value_column(row) for row in rows]

    df = pd.DataFrame(rows).set_index("tag")
    buf = io.StringIO()
    print_df_custom(df, max_colwidth=args.max_colwidth, pretty=not args.compact, file=buf)
    if picked_file is not None:
        print(picked_file.resolve(), file=buf)
    output_text = buf.getvalue()

    output_file = output_path_for(path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(output_text)
    print(f"Wrote {output_file}")
    return True

# ===== DETAILED IMPLEMENTATION =====================
def read_dataset(filepath: Path) -> pydicom.Dataset:
    return pydicom.dcmread(filepath, stop_before_pixels=True)

def list_directory_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.dcm") if p.is_file())

def pick_representative_file(files: list[Path]) -> Path:
    # Abstracted so the selection strategy can be swapped later. Currently: oldest
    # file by modification time. Caller must ensure `files` is non-empty.
    return min(files, key=lambda p: p.stat().st_mtime)

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
    ds: pydicom.Dataset,
    filter_paths: list[str] | None,
    pseudotags: bool = False,
    combo_groups: list[list[str]] | None = None,
) -> list[dict]:
    context = NameContext(ds)
    if filter_paths:
        elements = index_elements(ds)
        tags = load_tag_list(filter_paths)
        if combo_groups:
            # A --combos tag not covered by --filters would otherwise silently
            # never get computed, making that combo slot always None.
            seen = set(tags)
            for tag in combo_real_tags(combo_groups):
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
        rows = [build_row(context, tag, elements.get(tag)) for tag in tags]
    else:
        rows = [build_row(context, elem.tag, elem) for elem in ds.iterall()]
    if pseudotags:
        rows += compute_pseudotags(ds)
    return rows

def append_single_snapshot_combos(rows: list[dict], combo_groups: list[list[str]]) -> list[dict]:
    """--combos for a single dataset (plain file, or -1's representative file) --
    trivially one "file" per combo, but reuses the same directory-mode machinery
    for consistent rendering."""
    if not combo_groups:
        return rows
    file_values_by_tag = {row["tag"]: {0: row["value"]} for row in rows}
    return rows + compute_combo_rows(combo_groups, file_values_by_tag)

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

def _build_rows_for_file(
    filepath: Path,
    filter_paths: list[str] | None,
    pseudotags: bool,
    combo_groups: list[list[str]] | None,
) -> list[dict]:
    # Note: combo_groups is passed through only so build_rows() unions the right
    # tags into this file's own filtered set -- the actual cross-file combo
    # counting happens once in build_rows_for_directory below, not per file.
    return build_rows(read_dataset(filepath), filter_paths, pseudotags, combo_groups)

def build_rows_for_directory(
    directory: Path,
    filter_paths: list[str] | None,
    pseudotags: bool = False,
    combo_groups: list[list[str]] | None = None,
) -> list[dict]:
    combo_groups = combo_groups or []
    combo_labels = {combo_tag_label(raw) for group in combo_groups for raw in group}

    tag_order: list[str] = []
    first_seen_row: dict[str, dict] = {}
    value_counts_by_tag: dict[str, Counter] = {}
    # Per-file raw values, retained only for tags referenced by --combos -- the
    # per-tag Counters below throw away which values co-occurred within the same
    # file, which is exactly what compute_combo_rows needs reconstructed afterward.
    file_values_by_tag: dict[str, dict[str, object]] = {label: {} for label in combo_labels}

    files = list_directory_files(directory)
    # Each file's parsing is independent until this aggregation step, and is the
    # dominant cost for large directories, so it's worth spreading across cores.
    # Capped at half the available CPUs to leave headroom for other work on the
    # machine while this runs.
    max_workers = max(1, (os.cpu_count() or 1) // 2)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        per_file_rows = executor.map(
            _build_rows_for_file,
            files,
            [filter_paths] * len(files),
            [pseudotags] * len(files),
            [combo_groups] * len(files),
        )
        for filepath, rows in zip(files, per_file_rows):
            file_key = str(filepath)
            for row in rows:
                tag = row["tag"]
                if tag in file_values_by_tag:
                    file_values_by_tag[tag][file_key] = row["value"]
                if tag not in first_seen_row:
                    tag_order.append(tag)
                    first_seen_row[tag] = row
                    value_counts_by_tag[tag] = Counter()
                value_counts_by_tag[tag][str(row["value"])] += 1

    aggregated = [
        {**first_seen_row[tag], "value": dict(value_counts_by_tag[tag])}
        for tag in tag_order
    ]
    aggregated += compute_combo_rows(combo_groups, file_values_by_tag)
    return aggregated

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())
