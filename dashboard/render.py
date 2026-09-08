"""
render.py — HTML rendering for the Bob the Tester dashboard.

Everything here is presentation only: no database access, no judgement.
Each panel takes rows that build.py already fetched and returns a string of
HTML.  Keeping the panels pure functions of their rows is what lets
`python -m dashboard.build` and `dashboard/serve.py` share one code path.

The page is rendered server-side on purpose.  A judge opening the report
with JavaScript disabled, or from a USB stick over file://, still sees every
number; app.js only adds run switching and auto-refresh on top.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Pipeline order (AGENTS.md §4).  Display order only — never a gate.
STAGE_ORDER = [
    "run_tests",
    "get_coverage",
    "list_uncovered",
    "validate_and_keep",
    "detect_smells",
    "run_flaky_check",
    "run_property_tests",
    "run_fuzz",
    "mutation_test",
    "explain_gaps",
    "store_explanation",
    "save_test_record",
]

STAGE_LABEL = {
    "run_tests": "0 · Run tests",
    "get_coverage": "0 · Coverage",
    "list_uncovered": "12 · Gap map",
    "validate_and_keep": "3 · Validate & keep",
    "detect_smells": "4 · Smells",
    "run_flaky_check": "6 · Flakiness",
    "run_property_tests": "7 · Properties",
    "run_fuzz": "8 · Fuzz",
    "mutation_test": "10 · Mutation",
    "explain_gaps": "13 · Gap data",
    "store_explanation": "13 · Gap narration",
    "save_test_record": "9 · Test records",
}

# Columns whose values are prose and must be allowed to wrap.
_WRAPPING = {
    "reason", "verdict", "description", "evidence", "exception", "message",
    "shrunk_input", "input_repr", "stack_top", "original", "mutated", "detail",
    "source_file", "test_file", "smell", "hint", "why",
}

# Distinct hues for chart series; the dashboard never plots more than four.
_PALETTE = ["#3b6fd4", "#e0a84a", "#1f8a4c", "#c0392b"]

# How long a run may sit open with no tool call before the page stops
# calling it "running".  Generous on purpose: a mutation stage on a large
# module can genuinely go quiet for several minutes.
STALE_AFTER_MS = 15 * 60 * 1000


# ─────────────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────────────

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_time(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(ms: float | None) -> str:
    if not ms:
        return "0s"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {seconds % 60:04.1f}s"


def card(title: str, body: str, sub: str = "") -> str:
    subtitle = f'<p class="sub">{sub}</p>' if sub else ""
    return f'<article class="card"><h2>{title}</h2>{subtitle}{body}</article>'


def metric(label: str, value: Any, delta: str = "", delta_dir: str = "flat") -> str:
    tail = f'<div class="delta {delta_dir}">{esc(delta)}</div>' if delta else ""
    return (
        '<div class="metric">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(value)}</div>{tail}</div>'
    )


def metrics(*items: str) -> str:
    return '<div class="metrics">' + "".join(items) + "</div>"


def note(kind: str, text: str) -> str:
    """kind ∈ ok | warn | bad | info — `text` may contain inline markup."""
    return f'<p class="note {kind}">{text}</p>'


def prose(text: str) -> str:
    """
    Render Bob's narration.

    Bob writes markdown; the page is HTML.  Rather than pull in a markdown
    dependency for three constructs, this escapes everything first and then
    re-introduces only the inline forms Bob actually uses — bold, inline
    code, and `-` bullets.  Escaping first is what keeps it safe: no markup
    from the database can survive into the page as markup.
    """
    out = esc(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", out)
    out = re.sub(r"(?m)^(\s*)- ", r"\1• ", out)
    return f'<div class="prose">{out}</div>'


def bar(fraction: float, kind: str = "") -> str:
    pct = max(0.0, min(1.0, float(fraction))) * 100
    classes = f"bar {kind}".strip()
    return f'<div class="{classes}"><span style="width:{pct:.1f}%"></span></div>'


def _cell(key: str, value: Any) -> str:
    if value is None or value == "":
        return '<td class="no">—</td>'
    if isinstance(value, bool):
        return ('<td class="yes">✓</td>' if value else '<td class="no">·</td>')
    if isinstance(value, float):
        return f'<td class="num">{value:.3f}</td>'
    if isinstance(value, int):
        return f'<td class="num">{value}</td>'
    text = str(value)
    if isinstance(value, (list, dict)):
        text = str(value)
    css = "wrap" if key in _WRAPPING or len(text) > 40 else ""
    shown = text if len(text) <= 300 else text[:297] + "…"
    title = f' title="{esc(text)}"' if shown != text else ""
    return f'<td class="{css}"{title}>{esc(shown)}</td>'


def table(rows: Sequence[Mapping[str, Any]],
          columns: Iterable[str] | None = None,
          empty: str = "No rows.") -> str:
    """
    Render row dicts as a table.

    Column order comes from `columns` when given, so panels keep control of
    what the reader sees first; missing keys are skipped rather than
    rendered as empty columns.
    """
    rows = list(rows)
    if not rows:
        return f'<p class="note info">{esc(empty)}</p>'

    cols = list(columns) if columns else list(rows[0].keys())
    cols = [c for c in cols if any(c in r for r in rows)]

    head = "".join(f"<th>{esc(c.replace('_', ' '))}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(_cell(c, r.get(c)) for c in cols) + "</tr>"
        for r in rows
    )
    return (
        '<div class="tablewrap"><table><thead><tr>' + head +
        "</tr></thead><tbody>" + body + "</tbody></table></div>"
    )


def details(summary: str, body: str, open_: bool = False) -> str:
    attr = " open" if open_ else ""
    return (
        f"<details{attr}><summary>{summary}</summary>"
        f'<div class="body">{body}</div></details>'
    )


def line_chart(labels: Sequence[Any], series: Mapping[str, Sequence[float]],
               height: int = 240) -> str:
    """
    Inline SVG line chart — no charting library, no network fetch.

    Colours come from the palette; axis text uses `currentColor` so the
    chart stays legible in both the light and the dark theme.
    """
    if not labels or not series:
        return '<p class="note info">No data to plot.</p>'

    width = 660
    pad_l, pad_r, pad_t, pad_b = 46, 14, 14, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [float(v) for vs in series.values() for v in vs]
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
        return pad_t + plot_h * (1 - (float(v) - lo) / span)

    parts = [
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' "
        "role='img' preserveAspectRatio='xMidYMid meet' "
        "style='color:currentColor'>"
    ]

    for frac in (0.0, 0.5, 1.0):
        value = lo + span * frac
        gy = y(value)
        parts.append(
            f"<line x1='{pad_l}' y1='{gy:.1f}' x2='{width - pad_r}' y2='{gy:.1f}' "
            "stroke='currentColor' stroke-opacity='.15' stroke-width='1'/>"
            f"<text x='{pad_l - 8}' y='{gy + 4:.1f}' font-size='11' "
            "text-anchor='end' fill='currentColor' fill-opacity='.6'>"
            f"{value:.0f}</text>"
        )

    for index, (name, vs) in enumerate(series.items()):
        colour = _PALETTE[index % len(_PALETTE)]
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vs))
        parts.append(
            f"<polyline points='{points}' fill='none' stroke='{colour}' "
            "stroke-width='2.5' stroke-linejoin='round' stroke-linecap='round'/>"
        )
        for i, v in enumerate(vs):
            parts.append(
                f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='3.5' fill='{colour}'>"
                f"<title>{esc(name)}: {float(v):.1f}</title></circle>"
            )
        parts.append(
            f"<text x='{width - pad_r}' y='{pad_t + 12 + index * 15}' font-size='12' "
            f"text-anchor='end' fill='{colour}'>{esc(name)}</text>"
        )

    for i, label in enumerate(labels):
        if n > 8 and i % max(1, n // 8) != 0 and i != n - 1:
            continue
        parts.append(
            f"<text x='{x(i):.1f}' y='{height - 9}' font-size='11' "
            "text-anchor='middle' fill='currentColor' fill-opacity='.6'>"
            f"{esc(label)}</text>"
        )

    parts.append("</svg>")
    return '<div class="chart">' + "".join(parts) + "</div>"


# ─────────────────────────────────────────────────────────────────────────────
# Panels
# ─────────────────────────────────────────────────────────────────────────────

def coverage_panel(points: list[dict[str, Any]], run: dict[str, Any]) -> str:
    """Coverage trend — the baseline-to-target story for one run."""
    if not points:
        return card("Coverage",
                    note("info", "No coverage measurements recorded for this run yet."),
                    "Executed lines over the run.")

    first = float(points[0]["percent"])
    last = float(points[-1]["percent"])
    target = run.get("coverage_target")

    delta = last - first
    head = metrics(
        metric("Baseline", f"{first:.1f}%"),
        metric("Current", f"{last:.1f}%", f"{delta:+.1f} pts",
               "up" if delta > 0 else "down" if delta < 0 else "flat"),
        metric("Target", f"{target:.0f}%" if target else "—"),
        metric("Measurements", len(points)),
    )

    labels = list(range(1, len(points) + 1))
    series: dict[str, list[float]] = {
        "coverage %": [float(p["percent"]) for p in points]
    }
    # The target is drawn as its own series so the gap to it is visible at a
    # glance — the single number a judge looks for on this panel.
    if target:
        series["target %"] = [float(target)] * len(points)

    verdict = ""
    if target and last >= target:
        verdict = note("ok", f"Target met: <b>{last:.1f}%</b> ≥ {float(target):.0f}%")
    elif target:
        verdict = note("warn", f"<b>{float(target) - last:.1f}</b> points short of "
                               f"the {float(target):.0f}% target.")

    body = head + line_chart(labels, series) + verdict + details(
        "Measurement detail",
        table(points, ["iteration", "source_file", "percent",
                       "covered_count", "uncovered_count"]),
    )
    return card("Coverage", body, "Did the tests execute the code?")


def mutation_panel(rows: list[dict[str, Any]], run: dict[str, Any]) -> str:
    """
    Mutation score — the project's headline metric.

    Coverage says the tests EXECUTED the code; the mutation score says they
    would NOTICE if it were wrong.  With two or more rows, the delta between
    the first and the last IS the MuTAP story: score before survivor-targeted
    regeneration, and after.
    """
    sub = "Would the tests notice if the code were wrong?"
    if not rows:
        return card("Mutation score", note("info", "No mutation run recorded yet."), sub)

    if not rows[-1].get("engine_available", 1):
        return card("Mutation score", note(
            "warn",
            "mutmut is not installed, so this stage degraded cleanly rather than "
            'failing. Install it with <code>pip install -e ".[pipeline]"</code>.'
        ), sub)

    first, last = rows[0], rows[-1]
    score = float(last["mutation_score"])
    target = run.get("mutation_target")

    delta = ""
    direction = "flat"
    if len(rows) > 1:
        change = (score - float(first["mutation_score"])) * 100
        delta = f"{change:+.1f} pts"
        direction = "up" if change > 0 else "down" if change < 0 else "flat"

    kind = "ok" if target and score >= float(target) else "warn" if score >= .5 else "bad"

    body = metrics(
        metric("Mutation score", f"{score * 100:.1f}%", delta, direction),
        metric("Killed", f"{last['killed']} / {last['total']}"),
        metric("Survived", last["survived_count"]),
        metric("Target", f"{float(target) * 100:.0f}%" if target else "—"),
    ) + bar(score, kind)

    if len(rows) > 1:
        body += note("ok", (
            f"MuTAP loop: <b>{float(first['mutation_score']) * 100:.1f}%</b> → "
            f"<b>{score * 100:.1f}%</b> after survivor-targeted regeneration "
            f"({len(rows)} mutation runs)."
        ))
    else:
        body += note("info", (
            "One mutation run so far. A second run after killing survivors is "
            "what makes the improvement visible."
        ))

    survivors = last.get("survivors") or []
    survived_count = int(last.get("survived_count") or 0)

    if not survived_count:
        body += note("ok", "No surviving mutants — every mutation was caught.")
        return card("Mutation score", body, sub)

    if not survivors:
        # The count and the per-mutant list come from different parts of the
        # mutmut output; trusting the empty list over a non-zero count would
        # announce a clean sweep that did not happen.
        body += note("warn", (
            f"<b>{survived_count} surviving mutant(s)</b>, but the per-mutant "
            "detail is missing from this run — mutmut reported a count without "
            "a listing."
        ))
        return card("Mutation score", body, sub)

    # `status` separates the two fixes: a weak assertion versus no test at
    # all.  Collapsing them would hide which action Bob needs to take.
    body += note("warn", (
        f"<b>{last['survived_count']} surviving mutant(s)</b> — "
        f"{last['not_covered']} on lines no test executed."
    ))
    body += table(survivors,
                  ["id", "line", "function", "status", "original", "mutated"])
    return card("Mutation score", body, sub)


def _light(calls: list[dict[str, Any]]) -> str:
    """
    Traffic light for a stage, from its calls.

    Deliberately distinguishes the two ways a stage can be unhappy, because
    the contract does (AGENTS.md §3):
      🔴 the tool itself failed (ok=false) — misconfiguration, not test quality
      🟡 the tool ran but halted the pipeline (proceed=false) — a real gate
      🟢 ran and cleared
    """
    if any(not c["ok"] for c in calls):
        return "🔴"
    if any(not c["proceed"] for c in calls):
        return "🟡"
    return "🟢"


def pipeline_panel(calls: list[dict[str, Any]]) -> str:
    """
    Traffic lights and scores per stage.

    Reads only `tool_calls`, so it works for any tool the team adds later: a
    stage appears the moment it is called once.  Anything unrecognised is
    appended rather than dropped, so a new tool is never silently invisible.
    """
    sub = "Every stage the run touched, in pipeline order."
    if not calls:
        return card("Pipeline stages",
                    note("info", "No tool calls logged for this run yet."), sub)

    by_tool: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        by_tool.setdefault(call["tool"], []).append(call)

    ordered = [t for t in STAGE_ORDER if t in by_tool]
    ordered += [t for t in by_tool if t not in STAGE_ORDER]

    rows = []
    for tool in ordered:
        tool_calls = by_tool[tool]
        scores = [float(c["score"]) for c in tool_calls]
        rows.append({
            "": _light(tool_calls),
            "stage": STAGE_LABEL.get(tool, tool),
            "tool": tool,
            "calls": len(tool_calls),
            "avg score": round(sum(scores) / len(scores), 3),
            "time": fmt_duration(sum(c["duration_ms"] for c in tool_calls)),
            "cached": sum(1 for c in tool_calls if c["replayed"]),
            "last verdict": tool_calls[-1]["verdict"],
        })

    body = table(rows)

    skipped = [t for t in STAGE_ORDER if t not in by_tool]
    if skipped:
        body += note("info", "Not called in this run: <code>" +
                     "</code>, <code>".join(esc(t) for t in skipped) + "</code>.")

    blocked = [c for c in calls if not c["proceed"]]
    if blocked:
        body += note("warn", (
            f"<b>{len(blocked)} call(s) returned proceed=false</b> — the pipeline "
            "was gated here. The skill must stop advancing and act on the verdict."
        ))
        body += table([{"tool": c["tool"], "verdict": c["verdict"]}
                       for c in blocked[-5:]])
    return card("Pipeline stages", body, sub)


def bugs_panel(bugs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> str:
    """
    Defects — the payoff panel.

    A generated test that fails because the CODE is wrong is a bug found,
    not a test to discard (AGENTS.md §4, classification (a)).  This panel is
    where that distinction becomes visible, beside the machine-found
    evidence that produced it.
    """
    if bugs:
        body = note("bad", f"<b>{len(bugs)} genuine defect(s)</b> reported by the suite.")
        for bug in bugs:
            title = (f"{esc(bug['test_name'] or 'test')} — "
                     f"<span class='filename'>{esc(bug['source_file'] or 'source')}</span>")
            inner = (
                f"<p><b>Stage:</b> {esc(bug['stage'] or '—')}</p>"
                f"<p><b>What went wrong:</b> {esc(bug['description'] or '—')}</p>"
            )
            if bug.get("evidence"):
                inner += f"<pre>{esc(bug['evidence'])}</pre>"
            body += details(title, inner, open_=len(bugs) == 1)
    else:
        body = note("info", (
            "No defects recorded yet. Bob reports one by calling "
            "<code>save_test_record</code> with <code>bug_found=true</code> — a "
            "failing test whose assertion is right and whose code is wrong."
        ))

    body += _counterexamples(calls)
    body += _crashes(calls)
    return card("Defects found", body,
                "Failing tests where the code is wrong, not the test.")


def _counterexamples(calls: list[dict[str, Any]]) -> str:
    property_calls = [c for c in calls if c["tool"] == "run_property_tests"]
    if not property_calls:
        return ""

    failures: list[dict[str, Any]] = []
    for call in property_calls:
        failures.extend((call.get("details") or {}).get("failures", []) or [])

    out = "<h3>Hypothesis counterexamples</h3>"
    if not failures:
        return out + note("ok", "Every property held for all generated examples.")
    return out + note("warn", (
        f"<b>{len(failures)} shrunk counterexample(s)</b> — each becomes a named "
        "regression test."
    )) + table(failures, ["property", "shrunk_input", "exception"])


def _crashes(calls: list[dict[str, Any]]) -> str:
    fuzz_calls = [c for c in calls if c["tool"] == "run_fuzz"]
    if not fuzz_calls:
        return ""

    out = "<h3>Fuzz crashes</h3>"
    latest = fuzz_calls[-1].get("details") or {}
    if not latest.get("engine_available", True):
        return out + note("info", (
            "Atheris is not installed — fuzzing was the pre-agreed first cut. "
            "Hypothesis still supplies machine-found counterexamples."
        ))

    crashes: list[dict[str, Any]] = []
    for call in fuzz_calls:
        crashes.extend((call.get("details") or {}).get("crashes", []) or [])

    if not crashes:
        return out + note("ok", (
            f"No crashes at {latest.get('execs_per_sec', 0)} execs/sec over "
            f"{latest.get('seconds', 0)}s."
        ))
    return out + note("warn", f"<b>{len(crashes)} crashing input(s)</b> found.") + \
        table(crashes, ["input_repr", "exception", "stack_top"])


def tests_panel(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> str:
    """
    Kept/discarded — the Assured-LLMSE filter made visible.

    A generated test is kept only if it builds, passes, and strictly
    increases coverage.  Showing the discards WITH their reasons is the
    point: a pipeline that silently drops candidates is indistinguishable
    from one that never generated them.
    """
    if not rows:
        body = note("info", "No candidate tests recorded for this run yet.")
    else:
        kept = [r for r in rows if r["kept"]]
        bugs = [r for r in rows if r["bug_found"]]
        body = metrics(
            metric("Kept", len(kept)),
            metric("Discarded", len(rows) - len(kept)),
            metric("Defects found", len(bugs)),
            metric("Rolled back", sum(1 for r in rows if r["rollback_performed"])),
        )
        body += table([{
            "test_name": r["test_name"],
            "stage": r["stage"],
            "outcome": "kept" if r["kept"] else "discarded",
            "coverage": (
                f"{r['coverage_before']:.1f}% → {r['coverage_after']:.1f}%"
                if r["coverage_before"] is not None
                and r["coverage_after"] is not None else "—"
            ),
            "rolled_back": bool(r["rollback_performed"]),
            "bug": bool(r["bug_found"]),
            "reason": r["reason"],
        } for r in rows])

    body += _flaky(calls)
    body += _smells(calls)
    return card("Generated tests — kept vs. discarded", body,
                "Kept only if it builds, passes, and adds coverage.")


def _flaky(calls: list[dict[str, Any]]) -> str:
    """Flakiness is a hard gate — a test that varies is worthless as a signal."""
    flaky_calls = [c for c in calls if c["tool"] == "run_flaky_check"]
    if not flaky_calls:
        return ""

    flaky: list[dict[str, Any]] = []
    consistently_failing: list[str] = []
    runs = 0
    for call in flaky_calls:
        detail = call.get("details") or {}
        runs = max(runs, int(detail.get("runs", 0) or 0))
        flaky.extend(detail.get("flaky", []) or [])
        consistently_failing.extend(detail.get("consistently_failing", []) or [])

    out = "<h3>Flakiness check</h3>"
    if not flaky:
        out += note("ok", f"No flaky tests across {runs} runs of the suite.")
    else:
        out += note("bad", f"<b>{len(flaky)} flaky test(s)</b> — hard-discarded.")
        out += table(flaky)

    if consistently_failing:
        # Reported apart from flakiness on purpose: these fail every time, so
        # they are deterministically broken, not unstable.
        out += note("info", (
            f"{len(consistently_failing)} test(s) failed in every run — "
            "deterministic failures, not flakiness: <code>"
            + "</code>, <code>".join(esc(t) for t in consistently_failing[:8])
            + "</code>."
        ))
    return out


def _smells(calls: list[dict[str, Any]]) -> str:
    smell_calls = [c for c in calls if c["tool"] == "detect_smells"]
    if not smell_calls:
        return ""

    findings: list[dict[str, Any]] = []
    for call in smell_calls:
        findings.extend((call.get("details") or {}).get("findings", []) or [])

    latest = smell_calls[-1].get("details") or {}
    out = "<h3>Test smells</h3>" + note("info", (
        f"Latest scan: {latest.get('tests_scanned', 0)} tests, smell score "
        f"<b>{latest.get('smell_score', 0)}</b>. Smells lower the score but "
        "never gate the pipeline."
    ))
    if findings:
        return out + table(findings)
    return out + note("ok", "No smells detected in the latest scan.")


def gaps_panel(explanations: list[dict[str, Any]],
               calls: list[dict[str, Any]]) -> str:
    """
    Bob's prose beside the code it explains.

    explain_gaps supplies the structure (Layer 2); Bob writes the narration
    (Layer 1); store_explanation persists it.  This panel is the last step:
    why the uncovered lines are still uncovered, in language a judge can read
    without opening the source.
    """
    if explanations:
        body = ""
        for entry in explanations:
            if entry.get("source_file"):
                body += f'<p class="filename">{esc(entry["source_file"])}</p>'
            body += prose(entry["text"])
    else:
        body = note("info", (
            "No explanation stored yet. Bob writes one at stage 13 and saves it "
            "with <code>store_explanation</code>."
        ))

    body += _gap_structure(calls)
    return card("Coverage gaps explained", body,
                "Why the uncovered lines are still uncovered.")


def _gap_structure(calls: list[dict[str, Any]]) -> str:
    """Raw gap structures from explain_gaps, for anyone who wants the detail."""
    gap_calls = [c for c in calls if c["tool"] == "explain_gaps" and c["ok"]]
    if not gap_calls:
        return ""

    gaps = ((gap_calls[-1].get("details") or {}).get("gaps") or [])
    if not gaps:
        return note("ok", "No uncovered regions remain in the analysed file.")

    inner = ""
    for gap in gaps:
        inner += (
            f"<p><b>{esc(gap.get('function'))}</b> — lines {esc(gap.get('start'))}–"
            f"{esc(gap.get('end'))} ({esc(gap.get('line_count'))} lines)</p>"
        )
        if gap.get("docstring"):
            inner += f'<p class="sub">{esc(gap["docstring"])}</p>'
        inner += f"<pre>{esc(gap.get('snippet', ''))}</pre>"
        # Signals are facts, not verdicts: the tool reports them and Bob
        # decides what category the gap belongs to.
        signals = {k: v for k, v in (gap.get("signals") or {}).items() if v}
        if signals:
            inner += ('<p class="filename">Signals: ' +
                      ", ".join(f"{esc(k)}={esc(v)}" for k, v in signals.items()) +
                      "</p>")
    return details(f"Uncovered regions ({len(gaps)})", inner)


def activity_panel(calls: list[dict[str, Any]], run: dict[str, Any]) -> str:
    """
    The full tool-call log, iteration and cost counters.

    Note on "Bobcoin": the MCP server is deterministic and never talks to a
    model, so it cannot observe Bob's token spend — Bobalytics is the
    authority there (P3).  What this panel reports instead is what the server
    CAN measure honestly: how many tool calls a run took, how long they took,
    and how many were served from the replay snapshot rather than executed.
    """
    sub = "Every tool call the run made, in order."
    if not calls:
        return card("Run activity",
                    note("info", "No tool calls logged for this run yet."), sub)

    total_ms = sum(c["duration_ms"] for c in calls)
    cached = sum(1 for c in calls if c["replayed"])
    failed = sum(1 for c in calls if not c["ok"])

    body = metrics(
        metric("Tool calls", len(calls)),
        metric("Tool time", fmt_duration(total_ms)),
        metric("Iterations",
               f"{run.get('iterations_used', 0)}/{run.get('max_iterations') or '—'}"),
        metric("Served from cache", f"{cached}/{len(calls)}"),
    )

    if failed:
        body += note("bad", (
            f"<b>{failed} tool call(s) returned ok=false</b> — the tool itself "
            "failed (misconfiguration or a crash), which is not the same as the "
            "tests being bad."
        ))

    body += table([{
        "#": i + 1,
        "at": fmt_time(c.get("created_at")),
        "tool": c["tool"],
        "ok": bool(c["ok"]),
        "proceed": bool(c["proceed"]),
        "score": float(c["score"]),
        "ms": c["duration_ms"],
        "cached": bool(c["replayed"]),
        "verdict": c["verdict"],
    } for i, c in enumerate(calls)])

    payloads = ""
    for i, call in enumerate(calls, start=1):
        blob = json.dumps(
            {"arguments": call.get("arguments"), "details": call.get("details")},
            indent=2, default=str,
        )
        # A single fuzz or mutation payload can run to megabytes; the page has
        # to stay openable, so long ones are cut with the cut made visible.
        if len(blob) > 12000:
            blob = blob[:12000] + "\n… truncated — full payload is in the database."
        payloads += details(
            f"#{i} · <code>{esc(call['tool'])}</code> — {esc(call['verdict'])}",
            f"<pre>{esc(blob)}</pre>",
        )
    body += "<h3>Raw payloads</h3>" + payloads
    return card("Run activity", body, sub)


# ─────────────────────────────────────────────────────────────────────────────
# Page assembly
# ─────────────────────────────────────────────────────────────────────────────

STATIC = Path(__file__).resolve().parent / "static"


def _asset(name: str) -> str:
    try:
        return (STATIC / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def derive_status(run: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
    """
    What to call this run, from the evidence rather than from the row alone.

    `runs.status` is only ever moved off 'running' by finish_run.  A run
    whose process died — a crashed session, a Ctrl-C, a pipeline that
    simply never called finish_run — therefore keeps claiming to be in
    progress for as long as the database exists.  Reporting that verbatim
    is how a dashboard ends up describing an August run as live in
    September.

    So: finished runs report what they were closed as; an open run with
    recent tool activity is genuinely running; an open run that has been
    silent past STALE_AFTER_MS is `incomplete`, and says why.
    """
    started = run.get("started_at") or 0
    finished = run.get("finished_at")
    last_activity = max([c.get("created_at") or 0 for c in calls] + [started])

    if finished:
        status = run.get("status") or "complete"
        kind = {"complete": "ok", "failed": "bad"}.get(status, "warn")
        return {
            "label": status,
            "kind": kind,
            "incomplete": False,
            "duration_ms": finished - started if started else None,
            "duration_label": "Duration",
            "last_activity": last_activity,
        }

    age = now_ms() - last_activity
    if age > STALE_AFTER_MS:
        return {
            "label": "incomplete",
            "kind": "warn",
            "incomplete": True,
            # Everything after the last tool call is unaccounted for, so the
            # honest span is start → last activity, not start → now.
            "duration_ms": last_activity - started if started else None,
            "duration_label": "Ran for",
            "last_activity": last_activity,
        }

    return {
        "label": run.get("status") or "running",
        "kind": "info",
        "incomplete": False,
        "duration_ms": None,
        "duration_label": "Duration",
        "last_activity": last_activity,
    }


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def run_label(run: dict[str, Any], status: Mapping[str, Any] | None = None) -> str:
    source = run.get("source_file") or "(no source recorded)"
    label = status["label"] if status else (run.get("status") or "?")
    return f"{fmt_time(run['started_at'])} · {Path(source).name} · {label}"


def kpi_strip(bundle: dict[str, Any]) -> str:
    """The eight numbers a judge should be able to read without scrolling."""
    run = bundle["run"]
    coverage = bundle["coverage"]
    mutation = [m for m in bundle["mutation"] if m.get("engine_available", 1)]
    calls = bundle["calls"]
    tests = bundle["tests"]

    status = derive_status(run, calls)
    status_delta = "never finished" if status["incomplete"] else ""
    duration = (
        fmt_duration(status["duration_ms"])
        if status["duration_ms"] is not None else "running…"
    )

    if coverage:
        first, last = float(coverage[0]["percent"]), float(coverage[-1]["percent"])
        cov = metric("Coverage", f"{last:.1f}%", f"{last - first:+.1f} pts",
                     "up" if last > first else "down" if last < first else "flat")
    else:
        cov = metric("Coverage", "—")

    if mutation:
        score = float(mutation[-1]["mutation_score"]) * 100
        change = ""
        direction = "flat"
        if len(mutation) > 1:
            diff = score - float(mutation[0]["mutation_score"]) * 100
            change = f"{diff:+.1f} pts"
            direction = "up" if diff > 0 else "down" if diff < 0 else "flat"
        mut = metric("Mutation", f"{score:.1f}%", change, direction)
    else:
        mut = metric("Mutation", "—")

    return '<div class="card kpis">' + metrics(
        metric("Status", status["label"], status_delta,
               "down" if status["incomplete"] else "flat"),
        metric(status["duration_label"], duration,
               "to last tool call" if status["incomplete"] else "", "flat"),
        cov,
        mut,
        metric("Tests kept", f"{sum(1 for t in tests if t['kept'])}/{len(tests)}"),
        metric("Defects", len(bundle["bugs"])),
        metric("Tool calls", len(calls)),
        metric("Tool time", fmt_duration(sum(c["duration_ms"] for c in calls))),
    ) + (
        f'<p class="filename">run_id {esc(run["run_id"])} · source '
        f'{esc(run.get("source_file") or "—")} · started {fmt_time(run["started_at"])}'
        f' · finished {fmt_time(run.get("finished_at"))}</p>'
    ) + (note("warn", (
        "<b>This run was never closed.</b> Nothing has happened since "
        f"{fmt_time(status['last_activity'])}, and no <code>finish_run</code> "
        "call ever stamped a result — the process almost certainly exited "
        "early. The numbers below are whatever the run had reached by then, "
        "not a finished result."
    )) if status["incomplete"] else "") + (
        note("info", "This run was recorded in <b>replay mode</b> — tool results "
                      "came from the cached snapshot.")
         if run.get("replay_mode") else "") + (
        note("info", esc(run["notes"])) if run.get("notes") else ""
    ) + "</div>"


def run_section(bundle: dict[str, Any], hidden: bool) -> str:
    run = bundle["run"]
    body = (
        kpi_strip(bundle)
        + '<div class="grid">'
        + coverage_panel(bundle["coverage"], run)
        + mutation_panel(bundle["mutation"], run)
        + "</div>"
        + '<div class="grid full">'
        + pipeline_panel(bundle["calls"])
        + bugs_panel(bundle["bugs"], bundle["calls"])
        + tests_panel(bundle["tests"], bundle["calls"])
        + gaps_panel(bundle["gaps"], bundle["calls"])
        + activity_panel(bundle["calls"], run)
        + "</div>"
    )
    attr = " hidden" if hidden else ""
    return (f'<section class="run" data-run-id="{esc(run["run_id"])}"{attr}>'
            f"{body}</section>")


def _shell(inner: str, fingerprint: str = "", topbar_extra: str = "",
           generated_at: str = "") -> str:
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bob the Tester — run report</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ctext y='26' font-size='26'%3E%F0%9F%A7%AA%3C/text%3E%3C/svg%3E">
<style>
{_asset("style.css")}
</style>
</head>
<body data-fingerprint="{esc(fingerprint)}">
<header class="topbar">
  <div class="brand">🧪 Bob the Tester</div>
  <div class="tagline">AI writes your tests; Bob the Tester proves they'd catch real bugs.</div>
  {topbar_extra}
</header>
<main>
{inner}
</main>
<footer>Generated {esc(stamp)} — static HTML, no server required.</footer>
<script>
{_asset("app.js")}
</script>
</body>
</html>
"""


def page(bundles: list[dict[str, Any]], snapshot: Mapping[str, Any],
         fingerprint: str = "", generated_at: str = "") -> str:
    """The whole report: every run in the page, one shown at a time."""
    options = "".join(
        '<option value="{id}">{label}</option>'.format(
            id=esc(b["run"]["run_id"]),
            label=esc(run_label(b["run"], derive_status(b["run"], b["calls"]))),
        )
        for b in bundles
    )
    replay_badge = (
        '<span class="badge warn">replay snapshot: '
        f'{snapshot.get("entries", 0)} cached result(s) across '
        f'{len(snapshot.get("tools", {}))} tool(s)</span>'
    )
    topbar = (
        f'<label class="badge" for="run-picker">Run</label>'
        f'<select id="run-picker">{options}</select>'
        '<span class="badge" id="live-badge" title="How this page updates — '
        'it says nothing about whether a run is in progress.">static page</span>'
        f"{replay_badge}"
    )
    inner = "".join(run_section(b, hidden=i > 0) for i, b in enumerate(bundles))
    return _shell(inner, fingerprint, topbar, generated_at)


def empty_page(message: str, generated_at: str = "") -> str:
    """Rendered when there is no database yet, or it holds no runs."""
    body = card("Nothing to show yet", note("info", message))
    return _shell(f'<div class="grid full">{body}</div>',
                  generated_at=generated_at)
