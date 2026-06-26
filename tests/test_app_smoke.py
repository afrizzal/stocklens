"""Smoke tests for the multi-page Streamlit app — without the Streamlit runtime.

Each page is executed by calling its ``main()`` with **stubbed** ``streamlit`` and
``altair`` modules: widget calls return their defaults and display calls are no-ops, so
the full loader → analytics → chart-spec path of every page runs and any real logic error
(bad column, broken merge, type error) surfaces — yet nothing imports the heavy Streamlit
runtime. This needs no optional extra and runs anywhere.

Skipped when the consolidated artifact is absent (e.g. early in CI, before
``python cli.py all`` builds it), so the unit-test phase never triggers a heavy rebuild.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_APP = _REPO / "app"
_PARQUET = _REPO / "out" / "consolidate_purchasing_agg.parquet"
_PAGES = [_APP / "viewer.py", *sorted((_APP / "pages").glob("*.py"))]

pytestmark = pytest.mark.skipif(
    not _PARQUET.is_file(),
    reason="consolidated artifact absent — run `python cli.py all` first",
)


class _Node:
    """No-op Streamlit element/container that returns widget defaults."""

    def __enter__(self) -> _Node:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def slider(self, label: str, *args: object, **kw: object) -> object:
        if "value" in kw:
            return kw["value"]
        return args[2] if len(args) >= 3 else (args[0] if args else 0)

    def selectbox(self, label: str, options: object, index: int = 0, **kw: object) -> object:
        opts = list(options)  # type: ignore[call-overload]
        return opts[index] if opts else None

    def multiselect(
        self, label: str, options: object, default: object = None, **kw: object
    ) -> object:
        return list(default) if default is not None else list(options)  # type: ignore[call-overload]

    def button(self, *a: object, **k: object) -> bool:
        return False

    def toggle(self, label: str, value: bool = False, **k: object) -> bool:
        return value

    def columns(self, spec: object, **k: object) -> list[_Node]:
        n = spec if isinstance(spec, int) else len(spec)  # type: ignore[arg-type]
        return [_Node() for _ in range(n)]

    def tabs(self, labels: object, **k: object) -> list[_Node]:
        return [_Node() for _ in labels]  # type: ignore[union-attr]

    def altair_chart(self, chart: object, **k: object) -> None:
        if hasattr(chart, "to_dict"):
            chart.to_dict()

    def stop(self) -> None:
        raise _StopPage

    def __getattr__(self, _name: str) -> object:
        return lambda *a, **k: None


class _StopPage(Exception):
    """Raised by the stubbed ``st.stop()``."""


class _AnyChart:
    """Universal chainable that swallows the whole altair builder API."""

    def __call__(self, *a: object, **k: object) -> _AnyChart:
        return self

    def __add__(self, o: object) -> _AnyChart:
        return self

    def __radd__(self, o: object) -> _AnyChart:
        return self

    def to_dict(self, *a: object, **k: object) -> dict[str, object]:
        return {}

    def __getattr__(self, _name: str) -> _AnyChart:
        return self


@pytest.fixture
def _stub_modules() -> Iterator[None]:
    """Install stub ``streamlit`` / ``altair`` into ``sys.modules`` (restored after)."""
    saved = {name: sys.modules.get(name) for name in ("streamlit", "altair", "_data")}

    node = _Node()
    st = types.ModuleType("streamlit")
    for attr in dir(_Node):
        if not attr.startswith("__"):
            setattr(st, attr, getattr(node, attr))
    st.sidebar = _Node()  # type: ignore[attr-defined]
    st.session_state = {}  # type: ignore[attr-defined]
    st.set_page_config = lambda *a, **k: None  # type: ignore[attr-defined]
    st.cache_data = lambda func=None, **k: func or (lambda f: f)  # type: ignore[attr-defined]
    st.cache_resource = st.cache_data  # type: ignore[attr-defined]
    st.__getattr__ = lambda name: getattr(node, name)  # type: ignore[attr-defined]

    alt = types.ModuleType("altair")
    alt.__getattr__ = lambda _name: _AnyChart()  # type: ignore[attr-defined]

    sys.modules["streamlit"] = st
    sys.modules["altair"] = alt
    sys.modules.pop("_data", None)  # force a fresh bind against the stub
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.mark.duckdb
@pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.stem)
def test_page_runs_without_error(page: Path, _stub_modules: None) -> None:
    """The page's ``main()`` executes its full data path with no exception."""
    spec = importlib.util.spec_from_file_location(f"_page_{page.stem}", page)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
