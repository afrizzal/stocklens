"""StockLens JSON API package — a thin FastAPI layer over :mod:`stocklens.analytics`.

The API is read-only: it serves the artifacts the locked pipeline produces
(``out/consolidate_purchasing_agg.parquet`` + the seeded ``stocklens.duckdb``) as a
queryable data product, reusing the exact same pure analytics functions the CLI and the
Streamlit viewer use. Run it with ``uv run uvicorn api.main:app --reload`` from the repo
root (after ``python cli.py all`` has built the artifacts).
"""
