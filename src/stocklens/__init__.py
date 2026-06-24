"""StockLens — a sanitized, standalone showcase of two production purchasing pipelines.

This package is the clean-room, public reconstruction of two internal data-engineering
jobs (a purchasing-consolidation pipeline and an aging-stock alert). Every live
side-effect of the originals — warehouse queries, object-store writes, BI publishes,
spreadsheet syncs, and email blasts — is replaced by purely local, network-free
equivalents backed by a seeded DuckDB database and committed synthetic CSVs.

The package root exposes the shared configuration layer used by every module:

* :class:`Rules` — a frozen dataclass holding the eight tunable sections of
  ``config/rules.toml`` (each a plain ``dict`` of locked keys, BUILD-CONTRACT §2).
* :func:`load_rules` — parse a ``rules.toml`` file into a :class:`Rules` instance.

Every analytic module (:mod:`stocklens.demand_classify`,
:mod:`stocklens.stock_position`, :mod:`stocklens.margin_turnover`,
:mod:`stocklens.consolidate`, :mod:`stocklens.aging_alert`) reads its thresholds from
a :class:`Rules` object rather than re-hardcoding any constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Rules", "load_rules"]

# Repo root = three levels up from this file (src/stocklens/__init__.py -> repo/).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = "config/rules.toml"

# The locked section names of config/rules.toml (BUILD-CONTRACT §2). Loading asserts
# every one is present so a truncated config fails loudly rather than at first use.
_REQUIRED_SECTIONS = (
    "aging",
    "turnover",
    "classification",
    "stock",
    "windows",
    "demand",
    "report",
    "paths",
)


@dataclass(frozen=True)
class Rules:
    """Immutable view over ``config/rules.toml`` — one ``dict`` per locked section.

    Each attribute mirrors a ``[section]`` table from the TOML file and carries that
    section's key/value pairs verbatim (BUILD-CONTRACT §2). The dataclass is frozen so
    a loaded configuration cannot be mutated mid-pipeline; modules read values by key
    (e.g. ``rules.windows["sales_lookback_days"]``).

    Attributes:
        aging: Aging-stock thresholds and category-split tokens.
        turnover: Turnover-ratio caps and the ``recur_tor`` fallback.
        classification: Weighted-score weights and the wide-std damp rule.
        stock: Warehouse exclusions, special-handling ids, and lead-time fallbacks.
        windows: Rolling-window day counts and lookback horizons.
        demand: Demand-rate floor and IQR / single-sample outlier factors.
        report: Report recipients / sender (metadata only) and output directory.
        paths: Database and committed-CSV paths.
    """

    aging: dict[str, Any]
    turnover: dict[str, Any]
    classification: dict[str, Any]
    stock: dict[str, Any]
    windows: dict[str, Any]
    demand: dict[str, Any]
    report: dict[str, Any]
    paths: dict[str, Any]


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file with the stdlib ``tomllib`` (py3.11+) or the ``tomli`` backport.

    The contract pins ``tomli`` as a py3.10 dependency (BUILD-CONTRACT §6.1), so the
    backport is always importable on the target runtime; the stdlib module is preferred
    when available.
    """
    try:  # pragma: no cover - branch depends on interpreter version
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - exercised on py3.10 (tomli backport)
        import tomli as tomllib  # type: ignore[no-redef]

    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_rules(path: str = _DEFAULT_CONFIG) -> Rules:
    """Load ``config/rules.toml`` into a frozen :class:`Rules` instance.

    Relative paths resolve against the repo root so the configuration is found
    regardless of the current working directory. Every locked section
    (:data:`_REQUIRED_SECTIONS`) must be present.

    Args:
        path: Path to the ``rules.toml`` file. Defaults to ``config/rules.toml``.

    Returns:
        A :class:`Rules` instance with one ``dict`` per locked section.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError: If a locked ``[section]`` is missing from the file.
    """
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (_REPO_ROOT / config_path).resolve()

    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")

    data = _load_toml(config_path)

    missing = [section for section in _REQUIRED_SECTIONS if section not in data]
    if missing:
        raise KeyError(
            f"config {config_path} is missing required section(s): {', '.join(missing)}"
        )

    return Rules(
        aging=dict(data["aging"]),
        turnover=dict(data["turnover"]),
        classification=dict(data["classification"]),
        stock=dict(data["stock"]),
        windows=dict(data["windows"]),
        demand=dict(data["demand"]),
        report=dict(data["report"]),
        paths=dict(data["paths"]),
    )
