# scripts/

Convenience scripts, mostly for exploring DICOM metadata. Not polished enough (yet)
to promote into a real Elucid repo as a tool. The centerpiece is `describe.py`,
which replaced two earlier one-off scripts (`print_all_dicom_tags.py`,
`describe_cardiac_phase_info.py` — both deleted, functionality absorbed).

## Environment

These scripts depend on `pydicom`, `pandas`, `pyyaml` — none of which are in the
system Python. Per standing instruction: **never install packages into the shared
venv below.** Instead, reuse `~/git/EVServer`'s poetry-managed venv, which already
has them:

```bash
source activate_evserver_env.sh
```

This resolves the venv path dynamically via `poetry env info --path` (run inside
`~/git/EVServer`), so it stays correct if that venv is ever recreated. It does *not*
currently sync VSCode's `python.defaultInterpreterPath` (that auto-sync behavior was
tried and then removed) — `.vscode/settings.json` in this repo has the interpreter
path hardcoded instead.

## Layout

```
describe.py              # the main tool — see below
src/
  pydicom_utils.py        # tag parsing/formatting, private-tag name resolution
  pandas_utils.py         # print_df_custom: the custom table renderer
  pseudotags.py           # derived "pseudotags" (currently: Siemens ScanOptions split)
  combos.py               # -o/--combos: unique cross-tag value combinations
  tqdmcustom.py           # dependency-free progress-bar fallback
taglists/                 # YAML tag lists consumed by describe.py's -f flag
deps/3p/dicom3tools/      # git submodule — vendor private-tag dictionaries (see below)
variants/                 # unrelated one-off scripts (e.g. delete_patient.py)
summarize_s3_export_folder.py  # superseded by `describe.py -p` (Siemens ScanOptions
                                 # gate/phase split) but not yet deleted
```

## `describe.py` usage

```
describe.py FILEPATH_IN [-n] [-e] [-f FILE_OR_TAG ...] [-u] [-s] [-p] [-1] [-w N] [-c] [-o TAG TAG ...]
```

`FILEPATH_IN` is either a single DICOM file or a directory:
- **File**: prints one row per tag (tag, name, VR, VM, value).
- **Directory** (default): recursively globs `*.dcm` (skips non-DICOM clutter like
  `.tar.gz`/lock files that live alongside series folders in real exports), reads
  every file **in parallel** (see below), and *aggregates* the value column into
  `{unique value: file count}` counts per tag — column header becomes "value counts
  (unique value -> file count)" to make that shape self-explanatory.
- **Directory + `-1`/`--one`**: instead of aggregating, picks one representative
  file (currently: oldest by mtime — this policy is isolated in
  `pick_representative_file()` specifically so it's easy to swap later) and runs
  the single-file path on it, printing its resolved path afterward.

Flags:
- `-n`/`--hide-null`: hide tags whose value is `None` (entirely absent from the file).
- `-e`/`--hide-empty`: hide tags with an empty-string value (present but blank —
  e.g. stripped by de-identification).
- `-u`/`--hide-uniform` (directory mode): hide tags where every file has the same value.
- `-s`/`--hide-scattered` (directory mode): hide tags where no two files share a
  value (everything's unique — e.g. `SOPInstanceUID`).
- `-f`/`--filters FILE_OR_TAG ...`: restrict output to specific tags. Accepts a mix
  of YAML file paths (each a plain list of tags — see `taglists/`) and/or bare tag
  strings directly on the command line. Tag-string parsing (`parse_tag_string` in
  `pydicom_utils.py`) is deliberately lenient: `0020,000D`, `(0020,000D)`,
  `0x0020,0x000D`, `( 0x0020 , 0x000D )` all parse the same, both on the CLI and
  inside YAML list entries. Names are still auto-resolved dynamically (not read from
  the YAML) — the YAML only pins *which* tags to show; comments are for humans.
  Requesting a tag absent from the file still produces a row (`value: None`),
  distinguishing "doesn't exist" from "exists but blank".
- `-p`/`--pseudotags`: append synthetic rows from `pseudotags.compute_pseudotags()`
  (currently: Siemens `ScanOptions` (0018,0022) split into `ScanOptionsGate` /
  `ScanOptionsPhase` by searching for `GATE_`/`TP`-prefixed tokens — deliberately
  *not* positional, since token order isn't fixed across reconstruction types).
  Gated on `Manufacturer` containing "SIEMENS"; no-op otherwise.
- `-w`/`--max-colwidth N` (default 100): truncate cell values to N chars.
- `-c`/`--compact`: pretty-printing (box-drawn table, JSON-indented dict cells) is
  the *default*; this opts back into the old flat/whitespace-aligned rendering.
- `-o`/`--combos TAG TAG ...`: 2+ tags (real tags and/or pseudotag names, same
  flexible parsing as `-f`) to find the unique *joint* combinations of values for,
  across files in directory mode. Produces one extra row, `tag` =
  `COMBO(label1,label2,...)`, `value` = `{str(value_tuple): file_count}` — the same
  shape as an ordinary aggregated row, so it flows through `-n`/`-e`/`-u`/`-s`/
  pretty-printing with no special-casing. Repeatable for independent combo groups.
  This is `summarize_s3_export_folder.py`'s "Gate/phase detail" table, generalized
  to any pair (or n-tuple) of tags. If a group references a pseudotag name, `-p`
  must also be passed or that slot is always `None` (no implicit auto-enabling).
  A referenced *real* tag not otherwise covered by `-f` gets unioned into the
  filtered set automatically (see below), so you don't have to redundantly list it
  in both places.

## Non-obvious design decisions

**Private-tag naming defers to `dicom3tools`, not pydicom's bundled dictionary.**
`deps/3p/dicom3tools` is a git submodule (David Clunie's vendor private-tag
dictionaries, one `.tpl` file per vendor under `libsrc/standard/elmdict/`) — far
more complete than what pydicom ships. `pydicom_utils.py` parses these `.tpl` files
directly (`_load_tpl`, regex-based) rather than going through pydicom's API.
Currently only `gems.tpl` (GE) is wired up via `MANUFACTURER_TPL_FILES`; adding
another vendor is one dict entry.

**Git submodule gotcha already hit once**: `.gitmodules` and the tree's `160000`
gitlink entry are two *separate* things that must both be committed — `git add
<file1> <file2>` without the submodule path silently produces a repo where
`.gitmodules` references a submodule with no pinned commit, and `git submodule
init` will silently do nothing (there's no gitlink for it to act on). If a
submodule ever seems "broken" after a commit, check `git ls-tree HEAD --
<submodule-path>` for a `160000` entry before assuming the URL/path is wrong.

**GE private-creator guessing, and why it's gated so carefully.** Many exports have
their `(group,0010)` Private Creator elements blanked by de-identification, which
normally makes pydicom (and dicom3tools) unable to name anything in that group.
`GE_LEGACY_PRIVATE_CREATORS` in `pydicom_utils.py` hardcodes GE's decades-stable
group→creator convention for CT (confirmed against GE's actual Discovery/Revolution
CT conformance statements, not guessed) as a *fallback* — but only when the file's
own creator element is genuinely blank. If a group's creator is present but simply
unrecognized (e.g. one real fixture set has literal `"larry"` as the creator for
group 0009, from a nonstandard anonymizer), that real value always wins — never
silently overwritten by the guess. Every guessed (as opposed to read-from-file) name
is marked with a trailing `?` so it's visually distinguishable.

**Column widths, box-drawing, and JSON pretty-printing all had to solve the same
underlying problem**: a single outlier cell shouldn't force every other row in that
column to carry huge trailing padding (this caused a real "big blank gaps between
rows" bug when terminals soft-wrapped the wasted whitespace). `print_df_custom`
rstrips every printed line, and computes cell content as *lists of lines* (not
single strings) so a JSON-indented dict value can span multiple physical rows while
other columns render blank on the continuation lines — this is also what makes the
box-drawn "pretty" mode's per-row divider placement correct (one divider per
*logical* row, not per physical line).

**Dict-valued cells (`{value: file_count}` from directory aggregation) sort by the
recovered numeric type, not by string.** A tag like `Image Position (Patient)`
stringifies to `"[-69.300, -120.400, -122.750]"`; naive string sorting would put
`"[10.0, ...]"` before `"[2.0, ...]"`. `_dict_sort_key` in `pandas_utils.py` tries
`ast.literal_eval()` on each key to recover the original number/list-of-numbers and
sorts on that, falling back to string sort (in a separate tier, so numeric and
string keys are never compared directly) for genuinely non-numeric values. The
*displayed* string is untouched either way.

**Directory mode is parallelized across processes, not threads, and deliberately
capped.** Parsing DICOM files with pydicom is CPU-bound Python, so
`ThreadPoolExecutor` would've been serialized by the GIL — `ProcessPoolExecutor` is
used instead, each worker with its own interpreter/GIL. Capped at
`os.cpu_count() // 2` (not the full core count) to leave headroom on the machine;
the worker count is printed to stderr. The per-file worker function
(`_build_rows_for_file`) has to return plain picklable dicts (not e.g. a live
`pydicom.Dataset`) since everything crossing the process boundary gets pickled.

**Progress bar prints to stderr on purpose**, matching real `tqdm`'s own default —
this is *why* `describe.py ... > output.txt` cleanly separates the progress bar
(stays on your terminal) from the actual table (goes to the file): stdout and
stderr are independent streams, and `>` only redirects stdout. `tqdmcustom.py`
provides a dependency-free fallback (used automatically if `tqdm` isn't installed,
which it currently isn't in the shared venv) with the same stderr convention.

**`NameContext` exists purely for performance.** Profiling a slow directory run
showed `describe_name()` was recomputing `ds.get("Manufacturer")` and each private
group's creator lookup *once per DataElement* instead of once per file —
`pydicom.Dataset.__getitem__` alone was ~half the profiled runtime. `NameContext`
is built once per dataset and caches both.

**`row_is_all`/`row_is_uniform`/`row_is_scattered` all branch on `isinstance(value,
dict)`** to generalize single-file semantics ("value is None") to directory-mode's
aggregated counts ("every file agrees the value is None"), so the same `-n`/`-e`
flags work correctly in both modes without duplicated logic.

**`--combos` needed per-file values retained separately from the per-tag
Counters, because by the time the aggregated `{tag: {value: count}}` rows exist,
the information needed to reconstruct which *values of different tags* co-occurred
within the same file is already gone** — each tag's Counter is built independently.
`build_rows_for_directory` keeps a second structure, `file_values_by_tag` (`{tag
label: {file_path: raw_value}}`), populated *only* for tags actually referenced by
`--combos` (to avoid retaining this for every tag needlessly). `compute_combo_rows`
(`src/combos.py`) then joins across those per-tag dicts by file key to build the
`{value_tuple: count}` distribution once, after all files are processed — not
per-file. The tempting shortcut of "just emit a per-file combo row and let the
normal aggregation loop count it" does *not* work: each file's combo value would
already be a `{tuple: 1}` dict, and the generic aggregator would end up counting
occurrences of *that stringified single-entry dict* rather than merging the
underlying tuples — verified this fails before settling on the two-structure design.
Single-file and `-1` mode reuse the exact same `compute_combo_rows` via
`append_single_snapshot_combos`, just with one synthetic "file" key — no separate
code path needed.

## Known gaps / deliberately deferred

- `summarize_s3_export_folder.py` — its "Gate/phase detail" table is now subsumed
  by `-p -o ScanOptionsGate ScanOptionsPhase` (verified byte-for-byte equivalent
  counts against real Siemens fixture data). What's *not* yet subsumed: its
  "Folder summary" table, which runs across *many sibling folders in one
  invocation* (one row per folder: file count, distinct Study/SeriesUIDs, combo
  count) — `describe.py` only ever aggregates everything under one given root into
  a single flat report, with no per-subfolder breakdown. Not yet deleted.
- `--pseudotags`' Siemens gate/phase split doesn't have a GE equivalent yet (GE's
  cardiac phase lives in private CT Cardiac Sequence fields, group 0049 — already
  cataloged in `taglists/cardiac_phase.yaml`, just not turned into a pseudotag).
- No automated test suite. Verification throughout has been ad-hoc: real fixture
  data under `~/data/*-export/series_*/`, diffing output before/after refactors,
  `cProfile` when investigating performance claims rather than guessing.
