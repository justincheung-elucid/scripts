#!/usr/bin/env python3

import argparse
import os

import pydicom

SCAN_OPTIONS = (0x0018, 0x0022)
STUDY_UID = (0x0020, 0x000D)
SERIES_UID = (0x0020, 0x000E)


# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", help="Path to folder containing series subfolders")
    return parser.parse_args()


# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    series_dirs = sorted(
        entry
        for entry in os.listdir(args.data_root)
        if os.path.isdir(os.path.join(args.data_root, entry))
    )
    results = [scan_folder(args.data_root, folder) for folder in series_dirs]

    print("\nFolder summary\n")
    print_table(*folder_summary_table(results))

    print("\nGate/phase detail (one row per unique combo found in each folder)\n")
    print_table(*gate_phase_detail_table(results))


# ===== DETAILED IMPLEMENTATION =====================
def find_gate_token(scan_options):
    """Find the GATE_<n>_OF_<m> token, if present. Unlike scan_options[-1], this
    doesn't assume the gate marker is always the last token (it isn't for
    single-phase "BestSyst"/"BestDias" reconstructions, which end in RECONTYPE_TIME)."""
    for token in scan_options:
        if str(token).startswith("GATE_"):
            return str(token)
    return None


def scan_folder(data_root, folder_name):
    root_dir = os.path.join(data_root, folder_name)
    combos = set()
    studies = set()
    series = set()
    file_count = 0

    for relfp in sorted(os.listdir(root_dir)):
        ds = pydicom.dcmread(os.path.join(root_dir, relfp), stop_before_pixels=True)
        scan_options = ds[SCAN_OPTIONS].value if SCAN_OPTIONS in ds else None
        gate_str = (find_gate_token(scan_options) if scan_options else None) or "-"
        phase_str = scan_options[0] if scan_options else "-"
        study_uid = ds[STUDY_UID].value
        series_uid = ds[SERIES_UID].value

        combos.add((gate_str, phase_str, study_uid, series_uid))
        studies.add(study_uid)
        series.add(series_uid)
        file_count += 1

    return {
        "folder": folder_name,
        "file_count": file_count,
        "studies": studies,
        "series": series,
        "combos": combos,
    }


def short_uid(uid, head=100, tail=100):
    if len(uid) <= head + tail + 3:
        return uid
    return f"{uid[:head]}...{uid[-tail:]}"


def folder_summary_table(results):
    headers = ["Folder", "Files", "StudyUID", "SeriesUID", "#Gate/Phase combos"]
    rows = [
        [
            r["folder"],
            str(r["file_count"]),
            ", ".join(short_uid(s) for s in sorted(r["studies"])),
            ", ".join(short_uid(s) for s in sorted(r["series"])),
            str(len(r["combos"])),
        ]
        for r in results
    ]
    return headers, rows


def gate_phase_detail_table(results):
    headers = ["Folder", "Gate", "Phase (ScanOptions[0])", "StudyUID", "SeriesUID"]
    rows = [
        [r["folder"], gate_str, phase_str, short_uid(study_uid), short_uid(series_uid)]
        for r in results
        for gate_str, phase_str, study_uid, series_uid in sorted(r["combos"])
    ]
    return headers, rows


def print_table(headers, rows):
    widths = [
        max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt_row(cells):
        return "| " + " | ".join(cell.ljust(w) for cell, w in zip(cells, widths)) + " |"

    print(line)
    print(fmt_row(headers))
    print(line)
    for row in rows:
        print(fmt_row(row))
    print(line)


# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())
