#!/usr/bin/env python3

import argparse

import pydicom
import pandas as pd

from src.pydicom_utils import tag_to_string, describe_name
from src.pandas_utils import print_df_custom

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("filepath_in", help="Path to DICOM file")
    parser.add_argument(
        "--hide-blank",
        action="store_true",
        help="Hide tags whose value is blank/empty from the printed table",
    )
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    ds = pydicom.dcmread(args.filepath_in, stop_before_pixels=True)

    rows = [
        {
            "tag": tag_to_string(elem.tag),
            "name": describe_name(ds, elem),
            "VR": elem.VR,
            "VM": elem.VM,
            "value": elem.value if elem.VR != "SQ" else f"<Sequence, {len(elem.value)} item(s)>",
        }
        for elem in ds.iterall()
    ]
    if args.hide_blank:
        rows = [row for row in rows if row["value"] not in (None, "")]

    df = pd.DataFrame(rows).set_index("tag")
    print_df_custom(df)

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())
