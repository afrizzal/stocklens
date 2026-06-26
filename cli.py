"""StockLens command-line interface (Typer).

A single entry point that drives the standalone showcase pipeline end-to-end:

* ``seed``        — (re)build the synthetic ``stocklens.duckdb`` from ``seed/generate.py``.
* ``consolidate`` — run the demand / stock / margin / turnover consolidation and write
  ``out/consolidate_purchasing_agg.parquet`` (+ a ``.csv`` sibling).
* ``aging``       — run the aging-stock alert and render ``out/aging_report.html`` + ``.md``.
* ``all``         — ``seed`` → ``consolidate`` → ``aging`` in one shot.

Every value the original production jobs hardcoded is read from ``config/rules.toml``
through :func:`stocklens.load_rules`; nothing here re-hardcodes a tunable. There is no
network, SMTP, object-store, spreadsheet, or BI-server access anywhere in this CLI or the
code it calls — the originals' live side-effects are all replaced by local-file writes
(see ``docs/planning/BUILD-CONTRACT.md`` §0).

Runnable both as a plain script and through the installed console entry point::

    python cli.py all
    uv run python cli.py all
    stocklens all            # via [project.scripts] stocklens = "cli:app"

A ``--now`` ISO-date override and a ``--config`` path override are available on every
command for deterministic, reproducible runs (used by the tests).
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import sys
import time
from datetime import date
from pathlib import Path

import typer

# ── Path bootstrap ───────────────────────────────────────────────────────────
# When run as ``python cli.py`` (rather than via the installed package) the
# ``src/`` layout means ``stocklens`` is not yet importable. Add ``src`` (for the
# package) and the repo root (for ``shims`` / ``seed``) to ``sys.path`` so the CLI
# works on a clean checkout without an editable install.
_REPO_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _REPO_ROOT / "src"
for _p in (str(_SRC_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Ensure UTF-8 stdout/stderr so status glyphs (✓, •) render on legacy Windows
# consoles (cp1252), where the default encoding raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from shims import data_io  # noqa: E402  (import after sys.path bootstrap)
from stocklens import load_rules  # noqa: E402
from stocklens.aging_alert import run_aging_alert  # noqa: E402
from stocklens.consolidate import run_consolidate  # noqa: E402

_SEED_SCRIPT = _REPO_ROOT / "seed" / "generate.py"

logger = logging.getLogger("stocklens.cli")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="StockLens — synthetic inventory demand-classification & aging-stock pipeline.",
)


# ── Shared options / helpers ─────────────────────────────────────────────────

_CONFIG_OPTION = typer.Option(
    "config/rules.toml",
    "--config",
    "-c",
    help="Path to the rules TOML (defaults to config/rules.toml).",
)
_NOW_OPTION = typer.Option(
    None,
    "--now",
    help="ISO date (YYYY-MM-DD) to treat as 'today' for deterministic runs.",
)


def _configure_logging() -> None:
    """Set up friendly, single-line logging (idempotent across subcommands)."""
    root = logging.getLogger("stocklens")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s", "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _parse_now(now: str | None) -> date | None:
    """Parse the ``--now`` override into a :class:`datetime.date` (or ``None``)."""
    if now is None:
        return None
    try:
        return date.fromisoformat(now)
    except ValueError as exc:
        raise typer.BadParameter(f"--now must be an ISO date (YYYY-MM-DD); got {now!r}") from exc


def _load(config: str):
    """Load the :class:`stocklens.Rules` from ``config`` with a friendly error."""
    cfg_path = Path(config)
    if not cfg_path.is_absolute():
        cfg_path = _REPO_ROOT / cfg_path
    if not cfg_path.is_file():
        typer.secho(f"config not found: {cfg_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return load_rules(str(cfg_path))


def _seed(rules) -> Path:
    """Run ``seed/generate.py`` to (re)build the synthetic DuckDB; return its path.

    Imports the seed module by file path (it is a root-level script, not part of the
    installed package) and calls its ``generate(db_path, data_dir)`` entry point. The
    output DB path and the committed-CSV directory come from ``rules.paths`` so the seed
    and the rest of the pipeline always agree on locations.
    """
    if not _SEED_SCRIPT.is_file():
        typer.secho(f"seed script not found: {_SEED_SCRIPT}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    db_path = rules.paths["duckdb_path"]
    # The committed CSVs live alongside product_status.csv; derive their dir from config.
    data_dir = str(Path(rules.paths["product_status_csv"]).parent) or "data"

    spec = importlib.util.spec_from_file_location("stocklens_seed_generate", _SEED_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        typer.secho("could not load seed module", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    logger.info("seeding synthetic DuckDB -> %s (data dir: %s)", db_path, data_dir)
    out = module.generate(db_path, data_dir)
    return Path(out)


# ── Commands ─────────────────────────────────────────────────────────────────


@app.command()
def seed(
    config: str = _CONFIG_OPTION,
) -> None:
    """Generate the synthetic ``stocklens.duckdb`` from ``seed/generate.py``."""
    _configure_logging()
    t0 = time.perf_counter()
    rules = _load(config)
    db = _seed(rules)
    dt = time.perf_counter() - t0
    typer.secho(f"✓ seeded {db}  ({dt:.2f}s)", fg=typer.colors.GREEN)


@app.command()
def consolidate(
    config: str = _CONFIG_OPTION,
    now: str | None = _NOW_OPTION,
) -> None:
    """Run the consolidation pipeline -> ``out/consolidate_purchasing_agg.parquet``."""
    _configure_logging()
    t0 = time.perf_counter()
    rules = _load(config)
    run_now = _parse_now(now)

    con = data_io.connect(rules.paths["duckdb_path"])
    try:
        df = run_consolidate(con, rules, now=run_now)
    finally:
        con.close()

    out_dir = rules.report.get("output_dir", "out")
    parquet = Path(out_dir) / "consolidate_purchasing_agg.parquet"
    dt = time.perf_counter() - t0
    typer.secho(
        f"✓ consolidate: {len(df):,} grains -> {parquet}  ({dt:.2f}s)",
        fg=typer.colors.GREEN,
    )


@app.command()
def aging(
    config: str = _CONFIG_OPTION,
    now: str | None = _NOW_OPTION,
) -> None:
    """Run the aging-stock alert -> ``out/aging_report.html`` + ``.md``."""
    _configure_logging()
    t0 = time.perf_counter()
    rules = _load(config)
    run_now = _parse_now(now)

    con = data_io.connect(rules.paths["duckdb_path"])
    try:
        frames = run_aging_alert(rules, con, now=run_now)
    finally:
        con.close()

    out_dir = Path(rules.report.get("output_dir", "out"))
    counts = ", ".join(f"{name}={len(frame)}" for name, frame in frames.items())
    dt = time.perf_counter() - t0
    typer.secho(
        f"✓ aging: {counts} -> {out_dir / 'aging_report.html'} (+ .md)  ({dt:.2f}s)",
        fg=typer.colors.GREEN,
    )


@app.command()
def validate(
    config: str = _CONFIG_OPTION,
) -> None:
    """Run the data-quality contract over ``out/consolidate_purchasing_agg.parquet``.

    A CI-facing gate: loads the consolidated artifact and asserts the locked-schema
    guarantees (natural-key uniqueness, non-null grain keys, non-negative stock,
    bounded gross-margin rate, in-vocabulary categoricals — see
    :func:`stocklens.analytics.data_quality_checks`). Prints a per-check checklist and
    exits non-zero on any hard-check failure so a broken pipeline fails the build.
    """
    _configure_logging()
    from stocklens.analytics import validate_consolidated  # noqa: PLC0415

    rules = _load(config)
    out_dir = Path(rules.report.get("output_dir", "out"))
    if not out_dir.is_absolute():
        out_dir = _REPO_ROOT / out_dir
    parquet = out_dir / "consolidate_purchasing_agg.parquet"
    if not parquet.is_file():
        typer.secho(
            f"consolidated artifact not found: {parquet}\nrun `python cli.py consolidate` first",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # Read via DuckDB (no pyarrow dependency); the writer emits both .parquet and .csv.
    df = data_io.read_table(str(parquet))
    ok, results = validate_consolidated(df)
    for result in results:
        glyph = "✓" if result.passed else "✗"
        colour = typer.colors.GREEN if result.passed else typer.colors.RED
        tag = "" if result.hard else " (advisory)"
        typer.secho(f"  {glyph} {result.name}{tag}: {result.detail}", fg=colour)

    if not ok:
        typer.secho("✗ data-quality contract FAILED", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"✓ data-quality contract passed ({len(results)} checks)", fg=typer.colors.GREEN)


@app.command()
def all(  # noqa: A001 - "all" is the contract-mandated subcommand name
    config: str = _CONFIG_OPTION,
    now: str | None = _NOW_OPTION,
) -> None:
    """Run the full pipeline: ``seed`` → ``consolidate`` → ``aging``."""
    _configure_logging()
    t0 = time.perf_counter()
    rules = _load(config)
    run_now = _parse_now(now)

    # 1) seed ------------------------------------------------------------------
    db = _seed(rules)
    typer.secho(f"  • seeded {db}", fg=typer.colors.CYAN)

    # 2) consolidate -----------------------------------------------------------
    con = data_io.connect(rules.paths["duckdb_path"])
    try:
        df = run_consolidate(con, rules, now=run_now)
        out_dir = Path(rules.report.get("output_dir", "out"))
        parquet = out_dir / "consolidate_purchasing_agg.parquet"
        typer.secho(f"  • consolidate: {len(df):,} grains -> {parquet}", fg=typer.colors.CYAN)

        # 3) aging -------------------------------------------------------------
        frames = run_aging_alert(rules, con, now=run_now)
    finally:
        con.close()

    counts = ", ".join(f"{name}={len(frame)}" for name, frame in frames.items())
    typer.secho(
        f"  • aging: {counts} -> {out_dir / 'aging_report.html'} (+ .md)",
        fg=typer.colors.CYAN,
    )

    dt = time.perf_counter() - t0
    typer.secho(f"✓ all done  ({dt:.2f}s)", fg=typer.colors.GREEN)


def main() -> None:
    """Console-script / ``python cli.py`` entry point."""
    app()


if __name__ == "__main__":
    main()
