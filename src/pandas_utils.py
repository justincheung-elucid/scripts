import json

import pandas as pd

def print_df_custom(df: pd.DataFrame, max_colwidth: int = 100, pretty: bool = False):
    # to_string() won't cooperate: it ignores the global display.max_colwidth option
    # (unlike print(df), which uses __repr__), its `justify` option only affects
    # headers, and object-dtype columns get a leading space baked in by its internal
    # formatter (reserved for a sign character on numeric columns) even when a custom
    # formatter is supplied. Simplest to just build the table by hand.
    def format_cell(value) -> list[str]:
        # A column mixing ints/strings with None gets upcast by pandas (e.g. to
        # float64, turning None into nan) -- normalize back to "None" either way.
        # Can't use pd.isna(value) directly: it returns an elementwise array (not
        # a scalar) for list-like values such as multi-valued DICOM tags.
        is_missing = value is None or (isinstance(value, float) and pd.isna(value))
        if is_missing:
            return ["None"]
        if isinstance(value, dict):
            # Directory-mode aggregated {value: file_count} cells -- sort by key
            # for a stable, scannable order regardless of file-processing order.
            value = dict(sorted(value.items()))
            if pretty:
                # Indent like json.dumps(..., indent=4) instead of the flat repr.
                return json.dumps(value, indent=4, default=str).split("\n")
        s = str(value)
        if len(s) > max_colwidth:
            s = s[: max_colwidth - 3] + "..."
        return [s]

    def line_at(lines: list[str], i: int) -> str:
        return lines[i] if i < len(lines) else ""

    columns = list(df.columns)
    index_name = df.index.name or ""
    index_lines = [format_cell(v) for v in df.index]
    col_lines = {col: [format_cell(v) for v in df[col]] for col in columns}

    index_width = max(len(index_name), *(len(l) for lines in index_lines for l in lines))
    col_widths = {
        col: max(len(str(col)), *(len(l) for lines in col_lines[col] for l in lines))
        for col in columns
    }
    row_heights = [
        max(len(index_lines[i]), *(len(col_lines[col][i]) for col in columns))
        for i in range(len(df))
    ]

    if not pretty:
        header = index_name.ljust(index_width)
        header += "".join("  " + str(col).ljust(col_widths[col]) for col in columns)
        print(header.rstrip())
        for row_idx in range(len(df)):
            row = line_at(index_lines[row_idx], 0).ljust(index_width)
            row += "".join(
                "  " + line_at(col_lines[col][row_idx], 0).ljust(col_widths[col])
                for col in columns
            )
            # Padding out to the last column's global width is often mostly
            # whitespace for any given row -- trailing it here avoids wrapping to
            # a blank-looking continuation line in terminals narrower than that.
            print(row.rstrip())
        return

    widths = [index_width] + [col_widths[col] for col in columns]

    def border() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def box_row(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    print(border())
    print(box_row([index_name] + [str(col) for col in columns]))
    print(border())
    for row_idx in range(len(df)):
        for line_idx in range(row_heights[row_idx]):
            cells = [line_at(index_lines[row_idx], line_idx)] + [
                line_at(col_lines[col][row_idx], line_idx) for col in columns
            ]
            print(box_row(cells))
        print(border())
