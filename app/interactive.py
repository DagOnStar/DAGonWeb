"""DAGonWeb interactive task types.

Interactive tasks are a DAGonWeb extension: they are executed by DAGonStar as
ordinary ``TaskType.BATCH`` tasks, so every exported document still loads
with ``dagon.Workflow.load_json``. The DAGonWeb kind and its configuration
live in the task's additive ``dagonweb`` extension.

=========  ==============================================================
Kind       Behaviour
=========  ==============================================================
upload     Waits until a user uploads files on the run page, then copies
           them to ``outputs/``.
form       Waits until a user fills a form on the run page, then writes
           ``outputs/form.json`` and ``outputs/form.env``.
table      Turns a CSV/TSV/JSON input into ``outputs/table.json``,
           ``table.csv`` and ``table.html``; the run page renders it.
chart      Turns a CSV/TSV/JSON input into ``outputs/chart.json`` and a
           dependency-free ``outputs/chart.svg``; the run page renders it.
download   Packs its inputs into ``outputs/<archive>.zip`` and offers it
           as a download on the run page.
=========  ==============================================================

Commands reference two environment variables:

``DAGONWEB_DATA``
    Directory holding user-provided data, one sub-directory per task. The
    executor replaces it with ``<run>/_dagonweb/data``; the generated headless
    program exports it.
``DAGONWEB_PYTHON``
    Interpreter for the embedded helper scripts (``python3`` by default).

The module has no Flask or database dependencies.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any

INTERACTIVE_TYPES = ("upload", "form", "table", "chart", "download")
WAITING_TYPES = ("upload", "form")
RESULT_TYPES = ("table", "chart", "download")
DATA_PLACEHOLDER = "${DAGONWEB_DATA}"
PYTHON_PLACEHOLDER = "${DAGONWEB_PYTHON:-python3}"
FORM_FIELD_TYPES = ("text", "textarea", "number", "integer", "boolean", "select", "date", "email")
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CHART_TYPES = ("bar", "line", "scatter", "pie")

# ---------------------------------------------------------------------------
# Embedded helper scripts (standard library only, so headless bundles work
# with nothing but DAGonStar installed).
# ---------------------------------------------------------------------------

_LOADER = r'''
import csv, json, sys
from pathlib import Path


def load_rows(path):
    """Return (columns, rows) from a CSV, TSV or JSON file."""
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() in (".csv", ".tsv", ".txt"):
        delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames or []), rows
    value = json.loads(text)
    if isinstance(value, dict):
        if isinstance(value.get("rows"), list):
            value = value["rows"]
        elif all(isinstance(item, list) for item in value.values()) and value:
            keys = list(value)
            length = max(len(item) for item in value.values())
            value = [{key: (value[key][i] if i < len(value[key]) else None) for key in keys} for i in range(length)]
        else:
            lists = [item for item in value.values() if isinstance(item, list)]
            value = lists[0] if len(lists) == 1 else [value]
    if not isinstance(value, list):
        value = [value]
    rows = [row if isinstance(row, dict) else {"value": row} for row in value]
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns, rows


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
'''

TABLE_SCRIPT = _LOADER + r'''
import html

source, out_dir, title, wanted, limit = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5] or 1000)
columns, rows = load_rows(source)
if wanted.strip():
    columns = [c.strip() for c in wanted.split(",") if c.strip()]
rows = [[row.get(c) for c in columns] for row in rows[:limit]]
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "table.json").write_text(json.dumps({"title": title, "columns": columns, "rows": rows}, indent=2, default=str), encoding="utf-8")
with (out_dir / "table.csv").open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(columns)
    writer.writerows(rows)
cells = "".join("<tr>" + "".join(f"<td>{html.escape(str(v if v is not None else ''))}</td>" for v in row) + "</tr>" for row in rows)
head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
(out_dir / "table.html").write_text(f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><h1>{html.escape(title)}</h1><table border='1'><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table>\n", encoding="utf-8")
print(f"Table with {len(rows)} rows and {len(columns)} columns written to {out_dir}/table.json")
'''

CHART_SCRIPT = _LOADER + r'''
import html, math

source, out_dir, kind, x_key, y_keys, title = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
columns, rows = load_rows(source)
x_key = x_key or (columns[0] if columns else "")
y_list = [c.strip() for c in y_keys.split(",") if c.strip()] or [c for c in columns if c != x_key and any(number(r.get(c)) is not None for r in rows)][:4]
labels = [str(row.get(x_key, i)) for i, row in enumerate(rows)]
series = [{"name": y, "values": [number(row.get(y)) for row in rows]} for y in y_list]
spec = {"type": kind, "title": title, "x": x_key, "labels": labels, "series": series}
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "chart.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")

W, H, L, T, R, B = 720, 400, 60, 40, 20, 50
palette = ["#2457d6", "#c2410c", "#0f8a7e", "#7a4fd0", "#b07a12", "#d63343"]
parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="#fff"/><text x="{W/2}" y="22" text-anchor="middle" font-size="15">{html.escape(title)}</text>']
values = [v for s in series for v in s["values"] if v is not None]
if kind == "pie" and series:
    total = sum(v for v in series[0]["values"] if v and v > 0) or 1
    angle, cx, cy, r = -math.pi / 2, W / 2 - 80, H / 2 + 10, 140
    for i, (label, value) in enumerate(zip(labels, series[0]["values"])):
        if not value or value <= 0:
            continue
        step = 2 * math.pi * value / total
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        x2, y2 = cx + r * math.cos(angle + step), cy + r * math.sin(angle + step)
        large = 1 if step > math.pi else 0
        parts.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{palette[i % len(palette)]}"/>')
        parts.append(f'<rect x="{W-200}" y="{50 + i * 18}" width="10" height="10" fill="{palette[i % len(palette)]}"/><text x="{W-185}" y="{59 + i * 18}">{html.escape(label)} ({value:g})</text>')
        angle += step
elif values:
    low, high = min(0.0, min(values)), max(values)
    high = high if high > low else low + 1
    plot_w, plot_h = W - L - R, H - T - B
    count = max(1, len(labels))
    sx = lambda i: L + (i + 0.5) * plot_w / count
    sy = lambda v: T + plot_h - (v - low) / (high - low) * plot_h
    parts.append(f'<line x1="{L}" y1="{T + plot_h}" x2="{W - R}" y2="{T + plot_h}" stroke="#888"/><line x1="{L}" y1="{T}" x2="{L}" y2="{T + plot_h}" stroke="#888"/>')
    for tick in range(5):
        v = low + (high - low) * tick / 4
        parts.append(f'<text x="{L - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.3g}</text>')
    stride = max(1, count // 12)
    for i, label in enumerate(labels):
        if i % stride == 0:
            parts.append(f'<text x="{sx(i):.1f}" y="{T + plot_h + 16}" text-anchor="middle">{html.escape(label[:12])}</text>')
    bar_w = plot_w / count / max(1, len(series)) * 0.8
    for k, s in enumerate(series):
        color = palette[k % len(palette)]
        points = [(sx(i), sy(v)) for i, v in enumerate(s["values"]) if v is not None]
        if kind == "bar":
            for i, v in enumerate(s["values"]):
                if v is not None:
                    x = sx(i) - bar_w * len(series) / 2 + k * bar_w
                    parts.append(f'<rect x="{x:.1f}" y="{min(sy(v), sy(0)):.1f}" width="{bar_w:.1f}" height="{abs(sy(0) - sy(v)):.1f}" fill="{color}"/>')
        elif kind == "line" and points:
            parts.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (color, " ".join(f"{x:.1f},{y:.1f}" for x, y in points)))
        else:
            parts.extend(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>' for x, y in points)
        parts.append(f'<rect x="{L + 10 + k * 120}" y="{H - 16}" width="10" height="10" fill="{color}"/><text x="{L + 24 + k * 120}" y="{H - 7}">{html.escape(s["name"])}</text>')
parts.append("</svg>")
(out_dir / "chart.svg").write_text("".join(parts) + "\n", encoding="utf-8")
print(f"{kind} chart with {len(series)} series written to {out_dir}/chart.json and chart.svg")
'''

DOWNLOAD_SCRIPT = r'''
import sys, zipfile
from pathlib import Path

archive = Path(sys.argv[1])
archive.parent.mkdir(parents=True, exist_ok=True)
pairs = sys.argv[2:]
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
    for name, value in zip(pairs[0::2], pairs[1::2]):
        path = Path(value)
        if path.is_dir():
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    bundle.write(item, f"{name}/{item.relative_to(path)}")
        elif path.is_file():
            bundle.write(path, f"{name}/{path.name}")
        else:
            print(f"Skipping missing input {name}: {value}", file=sys.stderr)
print(f"Archive written to {archive}")
'''


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def _heredoc(script: str, arguments: list[str]) -> str:
    rendered = " ".join(arguments)
    return f'"{PYTHON_PLACEHOLDER}" - {rendered} <<\'DAGONWEB_PY\'\n{script.strip()}\nDAGONWEB_PY'


def _wait_block(name: str, timeout_minutes: Any) -> str:
    try:
        seconds = max(1, int(float(timeout_minutes or 60) * 60))
    except (TypeError, ValueError):
        seconds = 3600
    return (
        f'input_dir="{DATA_PLACEHOLDER}/{name}"\n'
        "waited=0\n"
        'while [ ! -f "$input_dir/.ready" ]; do\n'
        f"  if [ \"$waited\" -ge {seconds} ]; then echo \"Timed out waiting for user input for {name}\" >&2; exit 1; fi\n"
        "  sleep 2; waited=$((waited + 2))\n"
        "done"
    )


def safe_filename(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if SAFE_FILENAME.match(text) and ".." not in text else default


def interactive_command(task_type: str, name: str, config: dict[str, Any]) -> str:
    """Return the Batch script body (without input assignments) for a kind."""
    config = config or {}
    if task_type == "upload":
        return (
            _wait_block(name, config.get("timeout_minutes"))
            + '\nmkdir -p outputs\ncp -R "$input_dir/files/." outputs/\nls -l outputs'
        )
    if task_type == "form":
        return (
            _wait_block(name, config.get("timeout_minutes"))
            + '\nmkdir -p outputs\ncp "$input_dir/form.json" outputs/form.json\ncp "$input_dir/form.env" outputs/form.env\ncat outputs/form.json'
        )
    if task_type == "table":
        arguments = ['"$source"', "outputs", shlex.quote(str(config.get("title") or name)), shlex.quote(",".join(_columns(config.get("columns")))), str(_positive_int(config.get("max_rows"), 1000))]
        return "mkdir -p outputs\n" + _heredoc(TABLE_SCRIPT, arguments)
    if task_type == "chart":
        chart_type = config.get("chart_type") if config.get("chart_type") in CHART_TYPES else "bar"
        arguments = ['"$source"', "outputs", chart_type, shlex.quote(str(config.get("x") or "")), shlex.quote(",".join(_columns(config.get("y")))), shlex.quote(str(config.get("title") or name))]
        return "mkdir -p outputs\n" + _heredoc(CHART_SCRIPT, arguments)
    if task_type == "download":
        archive = safe_filename(config.get("archive"), "results.zip")
        inputs = config.get("inputs") if isinstance(config.get("inputs"), dict) else {}
        pairs = [f'{key} "${key}"' for key in inputs if SAFE_IDENTIFIER.match(str(key))]
        return _heredoc(DOWNLOAD_SCRIPT, [f"outputs/{archive}", *pairs])
    raise ValueError(f"Unsupported interactive task type {task_type}")


def _columns(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[\n,]+", str(value or "")) if item.strip()]


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def produced_files(task_type: str, config: dict[str, Any]) -> list[str]:
    """Files an interactive task always writes, for editor suggestions."""
    if task_type == "upload":
        return [f"outputs/{safe_filename(config.get('save_as'), 'data.csv')}"] if not config.get("multiple") else ["outputs"]
    if task_type == "form":
        return ["outputs/form.json", "outputs/form.env"]
    if task_type == "table":
        return ["outputs/table.json", "outputs/table.csv", "outputs/table.html"]
    if task_type == "chart":
        return ["outputs/chart.json", "outputs/chart.svg"]
    if task_type == "download":
        return [f"outputs/{safe_filename(config.get('archive'), 'results.zip')}"]
    return []


def substitute_runtime(command: str, data_dir: str, python: str) -> str:
    """Bind the placeholders of an interactive command to a concrete run.

    Placeholders always appear inside double quotes, so the values are
    escaped for that context.
    """
    def quoted(value: str) -> str:
        return re.sub(r'([\\"$`])', r"\\\1", value)

    return command.replace(DATA_PLACEHOLDER, quoted(data_dir)).replace(PYTHON_PLACEHOLDER, quoted(python))


# ---------------------------------------------------------------------------
# Form handling (shared by the run page and the headless program)
# ---------------------------------------------------------------------------


def form_fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sanitized form field definitions of a form task."""
    fields = []
    for raw in config.get("fields") or []:
        if not isinstance(raw, dict) or not SAFE_IDENTIFIER.match(str(raw.get("name", ""))):
            continue
        kind = raw.get("type") if raw.get("type") in FORM_FIELD_TYPES else "text"
        field = {
            "name": str(raw["name"]),
            "label": str(raw.get("label") or raw["name"]),
            "type": kind,
            "required": bool(raw.get("required", False)),
            "default": raw.get("default", False if kind == "boolean" else ""),
            "help": str(raw.get("help") or ""),
        }
        if kind == "select":
            field["options"] = [str(option) for option in raw.get("options") or []]
        fields.append(field)
    return fields


def coerce_form_values(fields: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any]:
    """Validate submitted values against the field definitions."""
    values: dict[str, Any] = {}
    for field in fields:
        name, kind = field["name"], field["type"]
        value = raw.get(name)
        if kind == "boolean":
            values[name] = str(value).lower() in {"1", "true", "on", "yes", "y"} if value is not None else bool(field.get("default"))
            continue
        text = "" if value is None else str(value).strip()
        if not text:
            if field["required"]:
                raise ValueError(f"{field['label']} is required.")
            values[name] = field.get("default", "")
            continue
        if len(text) > 10_000:
            raise ValueError(f"{field['label']} is too long.")
        if kind == "integer":
            try:
                values[name] = int(text)
            except ValueError:
                raise ValueError(f"{field['label']} must be an integer.") from None
        elif kind == "number":
            try:
                values[name] = float(text)
            except ValueError:
                raise ValueError(f"{field['label']} must be a number.") from None
        elif kind == "select" and field.get("options") and text not in field["options"]:
            raise ValueError(f"{field['label']} must be one of {', '.join(field['options'])}.")
        else:
            values[name] = text
    return values


def form_env(values: dict[str, Any]) -> str:
    """Render form values as ``export NAME=value`` lines for shell tasks."""
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"export {key.upper()}={shlex.quote(str(value))}")
    return "\n".join(lines) + "\n"


def form_document(values: dict[str, Any]) -> str:
    return json.dumps(values, indent=2, sort_keys=True) + "\n"
