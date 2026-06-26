"""Tests for the FastAPI JSON layer (:mod:`api.main`).

Drives the API through Starlette's ``TestClient`` (no live server) against the real
artifacts, asserting each endpoint returns well-formed, JSON-safe payloads from the
shared analytics engine. Skipped when FastAPI (the optional ``api`` extra) is absent or
the consolidated artifact has not been built, so the default CI run stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the optional `api` extra")

from api.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_PARQUET = Path(__file__).resolve().parents[1] / "out" / "consolidate_purchasing_agg.parquet"

pytestmark = [
    pytest.mark.duckdb,
    pytest.mark.skipif(
        not _PARQUET.is_file(),
        reason="consolidated artifact absent — run `python cli.py all` first",
    ),
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["grains"] > 0
    assert body["rows"] >= body["grains"]  # ≥1 window×outlier row per grain


def test_kpis(client: TestClient) -> None:
    body = client.get("/kpis").json()
    assert body["grains"] > 0
    assert "gmv" in body and "dead_stock_value" in body
    assert body["inventory_value_at_cost"] >= 0


def test_grains_pagination(client: TestClient) -> None:
    body = client.get("/grains", params={"limit": 5}).json()
    assert body["limit"] == 5
    assert len(body["items"]) <= 5
    assert body["total"] >= len(body["items"])


def test_grains_warehouse_filter(client: TestClient) -> None:
    body = client.get("/grains", params={"warehouse": "North DC", "limit": 500}).json()
    assert all(row["warehouse_name"] == "North DC" for row in body["items"])


def test_demand_classification(client: TestClient) -> None:
    body = client.get("/demand/classification").json()
    assert sum(body["distribution"].values()) > 0
    assert body["by_warehouse"]


def test_stock_reorder(client: TestClient) -> None:
    body = client.get("/stock/reorder").json()
    assert body["count"] == len(body["items"])


def test_aging(client: TestClient) -> None:
    body = client.get("/aging").json()
    assert body["value_at_risk"] >= 0
    assert "daily_needs" in body and "lifestyle" in body


def test_margin_gmroi(client: TestClient) -> None:
    body = client.get("/margin/gmroi", params={"limit": 3}).json()
    assert len(body["items"]) <= 3


def test_abc_xyz(client: TestClient) -> None:
    body = client.get("/abc-xyz").json()
    assert body["cells"]
    assert sum(body["abc_counts"].values()) > 0


def test_simulate_shifts_mix(client: TestClient) -> None:
    # Flip the weighting hard toward order-frequency to force reclassifications.
    resp = client.post("/simulate", json={"weight_qty": 0.1, "std_damp_factor": 0.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["grains"] > 0
    assert 0.0 <= body["changed_share"] <= 1.0
    assert sum(body["mix_before"].values()) == body["grains"]
    assert sum(body["mix_after"].values()) == body["grains"]


def test_openapi_schema(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "StockLens API"
    assert "/kpis" in schema["paths"]
