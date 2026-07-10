import pandas as pd

def print_df_custom(df: pd.DataFrame, max_colwidth: int = 100):
    # to_string() won't cooperate: it ignores the global display.max_colwidth option
    # (unlike print(df), which uses __repr__), its `justify` option only affects
    # headers, and object-dtype columns get a leading space baked in by its internal
    # formatter (reserved for a sign character on numeric columns) even when a custom
    # formatter is supplied. Simplest to just build the table by hand.
    def truncate(value) -> str:
        s = str(value)
        if len(s) > max_colwidth:
            return s[: max_colwidth - 3] + "..."
        return s

    index_name = df.index.name or ""
    index_cells = [truncate(v) for v in df.index]
    index_width = max(len(index_name), *(len(c) for c in index_cells))

    col_cells = {col: [truncate(v) for v in df[col]] for col in df.columns}
    col_widths = {
        col: max(len(str(col)), *(len(c) for c in col_cells[col])) for col in df.columns
    }

    header = index_name.ljust(index_width)
    header += "".join("  " + str(col).ljust(col_widths[col]) for col in df.columns)
    print(header)

    for row_idx, idx_cell in enumerate(index_cells):
        row = idx_cell.ljust(index_width)
        row += "".join(
            "  " + col_cells[col][row_idx].ljust(col_widths[col]) for col in df.columns
        )
        print(row)
