import pydicom

SCAN_OPTIONS_TAG = pydicom.tag.Tag(0x0018, 0x0022)

def compute_pseudotags(ds: pydicom.Dataset) -> list[dict]:
    """Synthetic rows derived by splitting a single tag's compound value into more
    directly meaningful sub-fields, in the same {tag, name, VR, VM, value} shape
    build_row() produces so they flow through the same aggregation/filtering as real
    tags. Dispatches to per-manufacturer/per-tag generators below -- add more here as
    new pseudotags are needed."""
    return _siemens_scan_options_pseudotags(ds)

def _find_token(scan_options, prefix: str) -> str | None:
    """Find the first token starting with `prefix`, if present. Deliberately not
    positional (e.g. not scan_options[0] or scan_options[-1]): token order isn't
    fixed across reconstruction types (single-phase "BestSyst"/"BestDias" recons
    end in RECONTYPE_TIME rather than a gate marker, for example)."""
    for token in scan_options:
        if str(token).startswith(prefix):
            return str(token)
    return None

def _pseudotag_row(tag: str, name: str, value) -> dict:
    return {"tag": tag, "name": f"[Pseudotag] {name}", "VR": None, "VM": None, "value": value}

def _siemens_scan_options_pseudotags(ds: pydicom.Dataset) -> list[dict]:
    if "SIEMENS" not in ds.get("Manufacturer", "").upper():
        return []
    if SCAN_OPTIONS_TAG not in ds:
        return []
    scan_options = ds[SCAN_OPTIONS_TAG].value
    if not scan_options:
        return []
    return [
        _pseudotag_row("ScanOptionsGate", "Scan Options Gate", _find_token(scan_options, "GATE_")),
        _pseudotag_row("ScanOptionsPhase", "Scan Options Phase", _find_token(scan_options, "TP")),
    ]
