"""Shared raw-text (CSV/JSON/NDJSON) table discovery for the s3 and file backends.

Both object-storage and local-directory discovery walk the same tree shape and
answer the same question — "which files fold into which logical table?" — so
the suffix classification, format naming, collision rules and the raw-size cap
live HERE once and both providers call into it (the providers keep their own
walk for Parquet/Delta; behavior for parquet-only directories is byte-identical
to before this module existed).

Landing-zone semantics (the design contract):

* Suffixes: ``.csv`` ``.tsv`` ``.json`` ``.ndjson`` ``.jsonl`` plus the
  ``.gz`` variant of each (``.csv.gz`` ``.json.gz`` ``.jsonl.gz``
  ``.ndjson.gz``). Anything else is not raw discovery's business.
* Size cap: ``SQLHANDLER_RAW_MAX_FILE_MB`` (default 64, 0 = unlimited). A raw
  table is DISCOVERED only if EVERY file that would feed it is at or under the
  cap; one oversized shard disqualifies the whole table (a partial table would
  lie), and the skip is logged once per table. For ``.gz`` files the cap
  applies to the COMPRESSED size — an uncompressed estimate would need a
  decompression pass per object at listing time — so the cap is approximate
  for gzip and a decompression bomb can exceed it.
* Precedence: Delta ``_delta_log`` folders win (the provider keeps raw files
  inside a Delta table out of discovery entirely), then Parquet wins over raw
  text in the same table folder (``table.csv`` in a folder of ``.parquet``
  files is ignored), and a raw-vs-raw collision on the same logical table
  (``events.csv`` + ``events.json``) resolves ALPHABETICALLY by format suffix
  with a warning — deterministic, and deterministic beats clever.
* Partition folders (``dt=2024`` style) fold into the parent table exactly as
  Parquet discovery folds them. pyarrow hive partitioning is deliberately OUT
  of scope: partition-COLUMN extraction stays Parquet-only, raw tables simply
  include the partition folders' files (the ``key=value`` directories are
  folded by directory, no columns are derived from the names).
* Open: a raw table's pyarrow Dataset is built over the EXPLICIT file list (a
  single-file table over that file, a folder table over its raw files) — never
  over the folder — because pyarrow's dataset factory with ``format="csv"``
  would otherwise also try to parse the ``.parquet`` files that share the
  folder (the Parquet-wins rule leaves them there) and fail the whole open.
"""

from __future__ import annotations

import logging

import pyarrow.csv as pacsv
import pyarrow.dataset as pad

from .config import RAW_GZ_SUFFIXES, RAW_TEXT_SUFFIXES, raw_formats_enabled, raw_max_file_mb

__all__ = [
    "build_raw_dataset",
    "classify_raw_file",
    "is_raw_format",
    "is_raw_suffix",
    "log_skipped",
    "raw_discovery_enabled",
    "raw_file_allowed",
    "raw_format_kind",
    "raw_size_cap_mb",
    "raw_table_within_cap",
]

logger = logging.getLogger("sqlhandler.rawfiles")

# The TableInfo.format strings raw discovery emits — the suffix (minus ".gz")
# is the format name, so "csv.gz" stays distinguishable from "csv" end to end.
GZ_SUFFIX = ".gz"


def classify_raw_file(name: str) -> str | None:
    """The raw format string for one file name, or None when not raw.

    The lowercase suffix decides: ``orders.CSV`` is raw csv (suffix case is
    not significant, exactly as ``_is_parquet`` treats it), ``orders.csv.gz``
    is ``"csv.gz"``, and ``orders.tsv.gz`` is ``"tsv.gz"``. Format strings
    keep the ``.gz`` marker so the engine's metadata gates (and the RAW
    badge) can treat compressed raw like the rest of the raw family.
    """
    lower = name.lower()
    for suffix in RAW_GZ_SUFFIXES:
        if lower.endswith(suffix):
            return suffix.lstrip(".")
    for suffix in RAW_TEXT_SUFFIXES:
        if lower.endswith(suffix):
            return suffix.lstrip(".")
    return None


def is_raw_suffix(name: str) -> bool:
    """True when a file name is a discoverable raw-text file (incl. ``.gz``)."""
    return classify_raw_file(name) is not None


def raw_discovery_enabled() -> bool:
    """The ``SQLHANDLER_RAW_FORMATS`` master switch (config.py owns the env)."""
    return raw_formats_enabled()


def raw_size_cap_mb() -> int:
    """The ``SQLHANDLER_RAW_MAX_FILE_MB`` cap (0 = unlimited)."""
    return raw_max_file_mb()


def raw_file_allowed(size_bytes: int, cap_mb: int) -> bool:
    """One file's size against the raw cap (``cap_mb`` 0 = unlimited)."""
    if cap_mb <= 0:
        return True
    return size_bytes <= cap_mb * 1024 * 1024


def raw_table_within_cap(sizes: list[int], cap_mb: int) -> bool:
    """Whether EVERY file feeding one raw table is at/under the cap.

    The cap is a table-level gate on purpose: a partitioned raw table with
    one oversized shard would otherwise list as a PARTIAL table (the small
    shards only), which lies about the data. Skipped is skipped whole.
    """
    return all(raw_file_allowed(size, cap_mb) for size in sizes)


def log_skipped(path: str, reason: str) -> None:
    """One info line per skipped raw table (the repo's listing-log style).

    Deliberately INFO (not debug): an operator pointing the backend at a
    landing zone deserves to see WHY a file sitting there is not queryable —
    the alternative is a silent "table not found" one hop later.
    """
    logger.info("skipping raw-format table %s: %s", path, reason)


def raw_format_kind(fmt: str) -> str:
    """The pyarrow dataset format a raw TableInfo.format string opens with.

    ``csv``/``tsv`` (+ ``.gz``) read as CSV — the tsv case gets a
    tab-delimited :class:`pyarrow.dataset.CsvFileFormat`; ``json``/
    ``ndjson``/``jsonl`` (+ ``.gz``) all read as pyarrow's ``json`` format,
    which is NEWLINE-DELIMITED JSON only (a single-line array document is out
    of scope — its open fails with the standard LakehouseError shape at query
    time, never a crash). ``.gz`` needs NO explicit compression handling on
    pyarrow 25: both the CSV and JSON dataset formats sniff the gzip magic on
    the opened stream (verified: ``pad.dataset('x.json.gz', format='json')``
    reads it plain), so the same format object serves both.
    """
    if fmt in ("tsv", "tsv.gz"):
        return "tsv"
    if fmt in ("json", "ndjson", "jsonl", "json.gz", "ndjson.gz", "jsonl.gz"):
        return "json"
    return "csv"


# The non-parquet formats rawfiles knows how to open. Used by the providers'
# open_dataset dispatch (everything that is neither delta nor one of these is
# plain Parquet) and by any caller that needs the raw-family test without
# hardcoding the suffix list.
RAW_FORMATS = frozenset(
    {
        "csv",
        "tsv",
        "json",
        "ndjson",
        "jsonl",
        "csv.gz",
        "tsv.gz",
        "json.gz",
        "ndjson.gz",
        "jsonl.gz",
    }
)


def is_raw_format(fmt: str) -> bool:
    """True when a TableInfo.format string is a raw-text format (incl. ``.gz``)."""
    return fmt in RAW_FORMATS


def build_raw_dataset(location: str, fmt: str, files: list[str], filesystem):
    """Open a raw table's EXPLICIT file list as one pyarrow Dataset.

    ``files`` are the source-relative paths collected at discovery time,
    passed as the dataset factory's source list (with the provider's
    filesystem), because a folder-form dataset with ``format="csv"`` would
    also try to parse the ``.parquet`` files the Parquet-wins rule leaves in
    the same table folder and fail the whole open. Schema inference happens
    naturally at open; any inference failure (mixed types in NDJSON, an empty
    CSV, a single-line-array .json) surfaces here as pyarrow's ArrowInvalid
    and the caller wraps it into the standard LakehouseError shape.
    """
    kind = raw_format_kind(fmt)
    if kind == "tsv":
        format_obj: object = pad.CsvFileFormat(pacsv.ParseOptions(delimiter="\t"))
    else:
        format_obj = kind
    return pad.dataset(files, filesystem=filesystem, format=format_obj)
