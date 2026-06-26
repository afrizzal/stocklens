"""Local data-access shims (DuckDB) — the open data layer.

The original production scripts pulled data from a cloud warehouse, wrote
results to an object store, and pushed an extract to a BI server. This module
replaces all of that with purely local, network-free equivalents:

* :func:`connect` opens the seeded ``stocklens.duckdb`` file.
* :func:`get_data` runs SQL (or reads a table) through DuckDB — the open
  stand-in for the warehouse query helper.
* :func:`read_sql_file` loads one of the ``src/stocklens/sql/*.sql`` queries.
* :func:`write_parquet` / :func:`write_csv` write to ``out/`` — the open
  stand-in for the object-store writer (no object store).
* :func:`ensure_seeded` (re)generates the DuckDB file by running
  ``seed/generate.py`` when it is missing or empty.
* :func:`publish_stub` is a no-op that replaces the BI-server publish.

There is no cloud warehouse, no object store, no BI server, and no network
access anywhere here.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger("stocklens.data_io")

# Repo root = two levels up from this file (shims/data_io.py -> repo/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SQL_DIR = _REPO_ROOT / "src" / "stocklens" / "sql"
_SEED_SCRIPT = _REPO_ROOT / "seed" / "generate.py"


def connect(db_path: str = "stocklens.duckdb") -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB database file and return a connection.

    Read-only callers pass the path to the already-seeded database. Relative
    paths are resolved against the repo root so the database is found
    regardless of the current working directory.

    Args:
        db_path: Path to the ``.duckdb`` file. Defaults to ``stocklens.duckdb``.

    Returns:
        An open DuckDB connection.
    """
    resolved = _resolve(db_path)
    return duckdb.connect(str(resolved))


def get_data(sql_or_name: str, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Run a query (or read a table / ``.sql`` file) against DuckDB.

    This is the open analogue of the original warehouse query helper — it
    only ever touches the local DuckDB connection and never reaches a network.

    ``sql_or_name`` is interpreted as follows:

    * ends with ``.sql`` (or is a path to an existing ``.sql`` file) → the file
      is loaded from ``src/stocklens/sql/`` (or the given path) and executed;
    * looks like SQL (contains whitespace or a leading SQL keyword) → executed
      verbatim;
    * otherwise → treated as a table name and run as ``SELECT * FROM <name>``.

    Args:
        sql_or_name: A raw SQL string, a ``.sql`` filename/path, or a table name.
        con: An open DuckDB connection (from :func:`connect`).

    Returns:
        The query result as a pandas DataFrame.
    """
    sql = _resolve_sql(sql_or_name)
    return con.execute(sql).df()


def read_sql_file(path: str) -> str:
    """Load a ``.sql`` file and return its text.

    Looks up the file under ``src/stocklens/sql/`` first (bare filenames), then
    falls back to treating ``path`` as a direct/relative path. The shipped SQL
    files already have all schema prefixes stripped (unqualified synthetic
    table names only).

    Args:
        path: A ``.sql`` filename (e.g. ``"orders.sql"``) or a path to one.

    Returns:
        The raw SQL text of the file.

    Raises:
        FileNotFoundError: If no matching ``.sql`` file exists.
    """
    candidate = _SQL_DIR / path
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")

    direct = _resolve(path)
    if direct.is_file():
        return direct.read_text(encoding="utf-8")

    raise FileNotFoundError(f"SQL file not found: {path!r} (looked in {_SQL_DIR} and {direct})")


def write_parquet(df: pd.DataFrame, path: str) -> str:
    """Write ``df`` to a local Parquet file under ``out/`` (no object store).

    The open analogue of the original object-store writer: writes a Parquet
    file and, for easy human inspection, a ``.csv`` sibling next to it. Parent
    directories are created as needed.

    Args:
        df: The DataFrame to persist.
        path: Destination Parquet path (relative paths resolve to the repo root).

    Returns:
        The absolute path of the Parquet file that was written.
    """
    parquet_path = _resolve(path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet_via_duckdb(df, parquet_path)

    csv_sibling = parquet_path.with_suffix(".csv")
    df.to_csv(csv_sibling, index=False)

    logger.info("wrote %d rows -> %s (+ %s)", len(df), parquet_path, csv_sibling.name)
    return str(parquet_path)


def read_table(path: str) -> pd.DataFrame:
    """Read a local Parquet artifact into pandas **via DuckDB** (no pyarrow needed).

    DuckDB is a locked runtime dependency and reads Parquet natively, so this avoids
    requiring an extra pandas Parquet engine (``pyarrow``/``fastparquet``) — mirroring
    :func:`write_parquet`, which writes via DuckDB. If the Parquet file is absent, falls
    back to the ``.csv`` sibling the writer always emits.

    Args:
        path: Path to the Parquet artifact (relative paths resolve to the repo root).

    Returns:
        The artifact as a pandas DataFrame.

    Raises:
        FileNotFoundError: If neither the Parquet file nor its ``.csv`` sibling exists.
    """
    parquet_path = _resolve(path)
    if parquet_path.is_file():
        con = duckdb.connect()
        try:
            target = parquet_path.as_posix()
            return con.execute(f"SELECT * FROM read_parquet('{target}')").df()  # noqa: S608
        finally:
            con.close()

    csv_sibling = parquet_path.with_suffix(".csv")
    if csv_sibling.is_file():
        return pd.read_csv(csv_sibling)

    raise FileNotFoundError(f"no artifact at {parquet_path} or its .csv sibling")


def write_csv(df: pd.DataFrame, path: str) -> str:
    """Write ``df`` to a local CSV file (no object store, no spreadsheets).

    Parent directories are created as needed. Used for small metadata sidecars
    (e.g. ``out/last_refreshed.csv``) that the originals pushed to a spreadsheet.

    Args:
        df: The DataFrame to persist.
        path: Destination CSV path (relative paths resolve to the repo root).

    Returns:
        The absolute path of the CSV file that was written.
    """
    csv_path = _resolve(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    logger.info("wrote %d rows -> %s", len(df), csv_path)
    return str(csv_path)


def ensure_seeded(db_path: str = "stocklens.duckdb") -> str:
    """Ensure the DuckDB database exists and is populated; seed it if not.

    Runs ``seed/generate.py`` when the database file is missing or contains no
    user tables. This keeps ``python cli.py consolidate`` / ``aging`` runnable
    on a clean checkout without a separate explicit seed step.

    Args:
        db_path: Path to the ``.duckdb`` file (relative paths resolve to repo root).

    Returns:
        The absolute path of the (now-seeded) database file.
    """
    resolved = _resolve(db_path)

    if _is_populated(resolved):
        logger.debug("database already seeded: %s", resolved)
        return str(resolved)

    logger.info("database missing/empty, seeding via %s", _SEED_SCRIPT)
    _run_seed(resolved)

    if not _is_populated(resolved):
        raise RuntimeError(
            f"seed step completed but {resolved} still has no tables; check seed/generate.py"
        )
    return str(resolved)


def publish_stub(name: str) -> None:
    """No-op replacement for the original BI-server publish step.

    The production pipeline built a BI extract and published it to a BI server
    project. In this showcase that side-effect is dropped entirely; this only
    logs an informational line so the orchestrator's intent stays visible.

    Args:
        name: The logical artifact name that *would* have been published.
    """
    logger.info("would publish %s (skipped: showcase build)", name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SQL_LEADING_KEYWORDS = (
    "select",
    "with",
    "pragma",
    "describe",
    "explain",
    "show",
    "call",
)


def _resolve(path: str) -> Path:
    """Resolve ``path`` against the repo root when it is relative."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def _write_parquet_via_duckdb(df: pd.DataFrame, parquet_path: Path) -> None:
    """Write ``df`` to Parquet using DuckDB's native writer.

    DuckDB is a locked runtime dependency and writes Parquet natively, so this
    avoids requiring an extra pandas Parquet engine (pyarrow/fastparquet).
    """
    con = duckdb.connect()
    try:
        con.register("_df_out", df)
        # Forward-slash the path so the SQL literal is platform-independent.
        target = parquet_path.as_posix()
        con.execute(
            f"COPY _df_out TO '{target}' (FORMAT PARQUET)"  # noqa: S608 - path is internal
        )
    finally:
        con.close()


def _resolve_sql(sql_or_name: str) -> str:
    """Turn a raw query, a ``.sql`` reference, or a table name into SQL text."""
    stripped = sql_or_name.strip()

    # A .sql file reference (bare name or path).
    if stripped.lower().endswith(".sql"):
        return read_sql_file(stripped)

    lowered = stripped.lower()
    looks_like_sql = any(lowered.startswith(kw) for kw in _SQL_LEADING_KEYWORDS) or (
        # A multi-token string is treated as a raw query; a single bare token
        # (no whitespace) is treated as a table name.
        any(ch.isspace() for ch in stripped)
    )
    if looks_like_sql:
        return stripped

    # Bare identifier -> table read.
    return f"SELECT * FROM {stripped}"


def _is_populated(db_path: Path) -> bool:
    """Return True if the DuckDB file exists and exposes at least one table."""
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except (duckdb.IOException, duckdb.Error):
        return False
    try:
        count = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchone()
        return bool(count and count[0] > 0)
    finally:
        con.close()


def _run_seed(db_path: Path) -> None:
    """Import and execute ``seed/generate.py`` to (re)build the database.

    The seed module is expected to expose ``main(db_path: str) -> None`` (or a
    ``generate(db_path)`` fallback). Importing by file path avoids depending on
    the repo being installed as a package.
    """
    if not _SEED_SCRIPT.is_file():
        raise FileNotFoundError(f"seed script not found: {_SEED_SCRIPT}")

    spec = importlib.util.spec_from_file_location("stocklens_seed_generate", _SEED_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load seed module from {_SEED_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    entry = getattr(module, "main", None) or getattr(module, "generate", None)
    if entry is None:  # pragma: no cover - defensive
        raise AttributeError("seed/generate.py must expose main(db_path) or generate(db_path)")
    entry(str(db_path))
