"""StockLens shims — open substitutes for the originals' internal infra.

The production pipelines this showcase reconstructs reached out to a cloud
warehouse for reads, an object store for writes, a BI server for publishing,
and SMTP/Sheets for distribution. None of that is appropriate (or safe) for a
public portfolio repo, so it is replaced here by two small, fully local
modules:

* :mod:`shims.data_io` — DuckDB-backed data access and local Parquet/CSV writes.
  It performs **no** network, object-store, or BI-server calls.
* :mod:`shims.report` — Jinja2 HTML/Markdown report rendering written to
  ``out/``, the open substitute for the original email/spreadsheet
  distribution. It sends **no** email and touches **no** spreadsheets.

A few convenience symbols are re-exported so callers can do
``from shims import connect, get_data, save_report``.
"""

from __future__ import annotations

from shims.data_io import (
    connect,
    ensure_seeded,
    get_data,
    publish_stub,
    read_sql_file,
    write_csv,
    write_parquet,
)
from shims.report import render_html, render_md, save_report

__all__ = [
    "connect",
    "ensure_seeded",
    "get_data",
    "publish_stub",
    "read_sql_file",
    "write_csv",
    "write_parquet",
    "render_html",
    "render_md",
    "save_report",
]
