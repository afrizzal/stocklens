"""Report rendering shims (Jinja2) — the open substitute for the email blast.

The original aging-stock script rendered an HTML email body (a greeting, a
"Daily Needs" table and a "Lifestyle" table) and mailed it to a list of
recipients, then pushed the full dataset to an external spreadsheet. None of
that belongs in a public portfolio, so this module renders the same report
shape to **local files** instead:

* :func:`render_html` — Jinja2 HTML report (generic greeting, no branding,
  no live links; recipients shown as *metadata only*).
* :func:`render_md` — the same report as Markdown.
* :func:`save_report` — render both and write ``out/aging_report.html`` and
  ``out/aging_report.md``.

There is no email send, no external spreadsheet write, and no network anywhere.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, select_autoescape

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The two named tables the original email body carried, in display order.
_TABLE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("daily_needs", "Daily Needs"),
    ("lifestyle", "Lifestyle"),
)

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>
  body { font-family: Arial, Helvetica, sans-serif; color: #222; margin: 2rem; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; margin-top: 1.5rem; }
  .meta { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
  table.table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; }
  table.table th, table.table td {
    border: 1px solid #ddd; padding: 6px 10px; text-align: center;
  }
  table.table-striped tbody tr:nth-child(odd) { background: #f7f7f7; }
  .empty { color: #999; font-style: italic; }
  .signature { margin-top: 2rem; color: #444; }
</style>
</head>
<body>
  <h1>{{ title }}</h1>
  <p class="meta">
    Generated: {{ generated_at }}<br>
    Would be sent from: {{ sender }}<br>
    Would be sent to: {{ recipients_display }}<br>
    <em>(showcase build — no email is sent; this report is written to disk.)</em>
  </p>

  <p>{{ greeting }},</p>
  <p>{{ intro }}</p>

  {% for key, heading in sections %}
  <h2>{{ heading }}</h2>
  {% if tables[key] %}
  {{ tables[key] | safe }}
  {% else %}
  <p class="empty">No aged stock in this category.</p>
  {% endif %}
  {% endfor %}

  <p class="signature">{{ signature }}</p>
</body>
</html>
"""

_MD_TEMPLATE = """\
# {{ title }}

_Generated: {{ generated_at }}_
_Would be sent from: {{ sender }}_
_Would be sent to: {{ recipients_display }}_
_(showcase build — no email is sent; this report is written to disk.)_

{{ greeting }},

{{ intro }}

{% for key, heading in sections %}
## {{ heading }}

{% if tables[key] %}
{{ tables[key] }}
{% else %}
_No aged stock in this category._
{% endif %}
{% endfor %}
{{ signature }}
"""

# Autoescape is on for HTML, but the table fragments are produced by pandas
# (df.to_html) and injected via the ``| safe`` filter, so they render as tables.
_env = Environment(autoescape=select_autoescape(["html", "xml"]))
_html_tpl = _env.from_string(_HTML_TEMPLATE)
_md_tpl = Environment(autoescape=False).from_string(_MD_TEMPLATE)


def render_html(context: dict[str, Any]) -> str:
    """Render the aging report as an HTML string.

    ``context`` carries report metadata and the two named tables. Each table in
    ``context["tables"]`` may already be an HTML fragment (a string, as produced
    by ``df.to_html(...)``) or a raw :class:`pandas.DataFrame`, which is
    converted with the standard ``table table-striped`` classes.

    Recognized ``context`` keys (all optional, with sensible defaults):
    ``title``, ``greeting``, ``intro``, ``signature``, ``sender``,
    ``recipients`` (list), ``generated_at``, and ``tables`` (a mapping with
    ``daily_needs`` / ``lifestyle`` entries).

    Args:
        context: The report context dict.

    Returns:
        The rendered HTML document as a string.
    """
    return _html_tpl.render(**_build_context(context, as_html=True))


def render_md(context: dict[str, Any]) -> str:
    """Render the aging report as a Markdown string.

    Mirrors :func:`render_html`; tables are rendered with ``df.to_markdown``
    (or passed through if already a Markdown string).

    Args:
        context: The report context dict.

    Returns:
        The rendered Markdown document as a string.
    """
    return _md_tpl.render(**_build_context(context, as_html=False))


def save_report(
    context: dict[str, Any],
    *,
    html_path: str = "out/aging_report.html",
    md_path: str = "out/aging_report.md",
) -> tuple[str, str]:
    """Render the report to HTML + Markdown and write both under ``out/``.

    This replaces the entire email-blast and external-spreadsheet-write block
    of the original. Parent directories are created as needed.

    Args:
        context: The report context dict (see :func:`render_html`).
        html_path: Destination for the HTML report (relative -> repo root).
        md_path: Destination for the Markdown report (relative -> repo root).

    Returns:
        A ``(html_path, md_path)`` tuple of the absolute paths written.
    """
    html = render_html(context)
    md = render_md(context)

    html_out = _resolve(html_path)
    md_out = _resolve(md_path)
    html_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)

    html_out.write_text(html, encoding="utf-8")
    md_out.write_text(md, encoding="utf-8")
    return str(html_out), str(md_out)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_context(context: dict[str, Any], *, as_html: bool) -> dict[str, Any]:
    """Normalize a caller context into the variables the templates expect."""
    recipients = context.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients_display = ", ".join(recipients) if recipients else "(none configured)"

    raw_tables = context.get("tables") or {}
    tables = {
        key: _render_table(raw_tables.get(key), as_html=as_html)
        for key, _heading in _TABLE_SECTIONS
    }

    return {
        "title": context.get("title", "Aging Stock Report"),
        "greeting": context.get("greeting", "Dear Purchasing Team"),
        "intro": context.get(
            "intro",
            "Below is the aged-stock summary by category, with sell-out over "
            "the recent lookback window.",
        ),
        "signature": context.get("signature", "Regards, Analytics"),
        "sender": context.get("sender", "reports@example.com"),
        "recipients_display": recipients_display,
        "generated_at": context.get(
            "generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ),
        "sections": list(_TABLE_SECTIONS),
        "tables": tables,
    }


def _render_table(value: Any, *, as_html: bool) -> str:
    """Convert a table value (DataFrame or pre-rendered string) to markup.

    Empty / missing tables return an empty string so the templates show the
    "No aged stock" placeholder instead of an empty grid.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return ""
        if as_html:
            return value.to_html(
                classes="table table-striped", index=False, justify="center"
            )
        return _df_to_markdown(value)
    # Unknown type: render its string form rather than crash.
    return str(value)


def _df_to_markdown(df: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavored Markdown table.

    Uses pandas' ``to_markdown`` (backed by ``tabulate``, a runtime dep) when
    available, and falls back to a minimal hand-rolled renderer otherwise so a
    missing optional dependency never breaks report generation.
    """
    try:
        return df.to_markdown(index=False)
    except ImportError:
        headers = [str(c) for c in df.columns]
        header_row = "| " + " | ".join(headers) + " |"
        divider = "| " + " | ".join("---" for _ in headers) + " |"
        body = [
            "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
            for row in df.itertuples(index=False, name=None)
        ]
        return "\n".join([header_row, divider, *body])


def _resolve(path: str) -> Path:
    """Resolve ``path`` against the repo root when it is relative."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()
