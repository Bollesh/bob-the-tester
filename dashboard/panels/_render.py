"""
Rendering helpers with an Arrow-free fallback.

Why this exists:
    st.dataframe() and st.line_chart() both serialise through pyarrow.  On a
    locked-down Windows box (Smart App Control / WDAC) the pyarrow native DLL
    can be blocked outright, and every table and chart on the page then
    raises ImportError — the whole dashboard dies on a machine that is
    otherwise perfectly capable of running it.

    Since the dashboard is the demo's visual centrepiece, it must not depend
    on a native library loading.  These helpers use Streamlit's rich widgets
    when Arrow is importable and degrade to hand-rendered HTML/SVG when it is
    not.  The fallback is plain markup: no extra dependency, no native code.

Everything here is presentation only — no data access, no judgement.
"""
from __future__ import annotations

import html
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

import streamlit as st


@lru_cache(maxsize=1)
def arrow_available() -> bool:
    """True when pyarrow imports, i.e. st.dataframe/st.line_chart will work."""
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "✓" if value else "·"
    text = str(value)
    return html.escape(text if len(text) <= 200 else text[:197] + "…")


def table(rows: Sequence[Mapping[str, Any]], columns: Iterable[str] | None = None) -> None:
    """
    Render a list of row dicts as a table.

    Streamlit's interactive dataframe when Arrow is available; a static HTML
    table otherwise.  Column order is taken from `columns` when given, so
    panels keep control of what the reader sees first.
    """
    rows = list(rows)
    if not rows:
        st.caption("No rows.")
        return

    cols = list(columns) if columns else list(rows[0].keys())

    if arrow_available():
        import pandas as pd

        frame = pd.DataFrame(rows)
        present = [c for c in cols if c in frame.columns]
        st.dataframe(frame[present], width="stretch", hide_index=True)
        return

    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(row.get(c))}</td>" for c in cols) + "</tr>"
        for row in rows
    )
    st.markdown(
        "<div style='overflow-x:auto'>"
        "<table style='width:100%;border-collapse:collapse;font-size:0.85rem'>"
        "<thead style='text-align:left;opacity:0.7'><tr>" + head + "</tr></thead>"
        "<tbody>" + body + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


def line_chart(labels: Sequence[Any], series: Mapping[str, Sequence[float]],
               height: int = 220) -> None:
    """
    Draw one or more numeric series against `labels`.

    Falls back to an inline SVG polyline chart when Arrow is unavailable.
    The SVG uses `currentColor` for text so it stays legible in both the
    light and dark Streamlit themes.
    """
    if not labels or not series:
        st.caption("No data to plot.")
        return

    if arrow_available():
        import pandas as pd

        frame = pd.DataFrame({name: list(values) for name, values in series.items()})
        frame.index = list(labels)
        st.line_chart(frame, height=height)
        return

    st.markdown(_svg_line_chart(labels, series, height), unsafe_allow_html=True)


# Distinct hues for up to four series; the dashboard never plots more.
_PALETTE = ["#4c8bf5", "#f5a623", "#2fb344", "#d1495b"]


def _svg_line_chart(labels: Sequence[Any], series: Mapping[str, Sequence[float]],
                    height: int) -> str:
    width = 640
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for vs in series.values() for v in vs]
    lo, hi = min(values), max(values)
    if hi == lo:
        # A flat series would divide by zero; give it room so the line sits
        # in the middle of the plot rather than on an edge.
        lo, hi = lo - 1, hi + 1
    span = hi - lo

    n = len(labels)

    def x(i: int) -> float:
        return pad_l + (plot_w if n == 1 else plot_w * i / (n - 1))

    def y(v: float) -> float:
        return pad_t + plot_h * (1 - (v - lo) / span)

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' "
        "role='img' style='color:currentColor'>"
    ]

    # Horizontal gridlines with value labels.
    for frac in (0.0, 0.5, 1.0):
        value = lo + span * frac
        gy = y(value)
        parts.append(
            f"<line x1='{pad_l}' y1='{gy:.1f}' x2='{width - pad_r}' y2='{gy:.1f}' "
            "stroke='currentColor' stroke-opacity='0.15' stroke-width='1'/>"
        )
        parts.append(
            f"<text x='{pad_l - 6}' y='{gy + 4:.1f}' font-size='10' "
            "text-anchor='end' fill='currentColor' fill-opacity='0.6'>"
            f"{value:.0f}</text>"
        )

    for index, (name, vs) in enumerate(series.items()):
        colour = _PALETTE[index % len(_PALETTE)]
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vs))
        parts.append(
            f"<polyline points='{points}' fill='none' stroke='{colour}' "
            "stroke-width='2' stroke-linejoin='round'/>"
        )
        for i, v in enumerate(vs):
            parts.append(
                f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='3' fill='{colour}'/>"
            )
        parts.append(
            f"<text x='{width - pad_r}' y='{pad_t + 12 + index * 14}' font-size='11' "
            f"text-anchor='end' fill='{colour}'>{html.escape(str(name))}</text>"
        )

    for i, label in enumerate(labels):
        if n > 8 and i % max(1, n // 8) != 0 and i != n - 1:
            continue
        parts.append(
            f"<text x='{x(i):.1f}' y='{height - 8}' font-size='10' "
            "text-anchor='middle' fill='currentColor' fill-opacity='0.6'>"
            f"{html.escape(str(label))}</text>"
        )

    parts.append("</svg>")
    return "<div style='overflow-x:auto'>" + "".join(parts) + "</div>"


def arrow_notice() -> None:
    """Explain the degraded rendering once, in the sidebar."""
    if arrow_available():
        return
    st.caption(
        "pyarrow is unavailable on this machine, so tables and charts are "
        "rendered as static HTML/SVG. All data is the same."
    )
