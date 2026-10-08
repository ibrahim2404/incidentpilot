"""Turn Prometheus results into short text for the model.

Pure functions (no network, no MCP) so they are easy to unit-test.
Every output is bounded: at most MAX_SERIES series and MAX_POINTS points per series.
"""

from __future__ import annotations

MAX_SERIES = 15
MAX_POINTS = 12
NOISY_LABELS = {"id", "name", "image", "uid"}

def labels(metric: dict) -> str:
    """{'__name__': 'up', 'job': 'pods', 'pod': 'orders-1'} -> 'up{job="pods", pod="orders-1"}'"""
    name = metric.get("__name__", "")
    rest = ", ".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__" and k not in NOISY_LABELS)
    return f"{name}{{{rest}}}" if rest else (name or "{}")


def number(text: str) -> str:
    """Prometheus sends values as text; show them short and readable."""
    value = float(text)
    if value != value:  # NaN
        return "NaN"
    if value == int(value) and abs(value) < 1e12:
        return str(int(value))
    return f"{value:.4g}"


def vector(result: list[dict]) -> str:
    """One line per series: labels = value."""
    if not result:
        return "No series matched. Either nothing happened, or the metric name or a label filter is wrong."
    lines = [f"{labels(s['metric'])} = {number(s['value'][1])}" for s in result[:MAX_SERIES]]
    if len(result) > MAX_SERIES:
        lines.append(f"... {len(result) - MAX_SERIES} more series not shown. Narrow the query with label filters or sum by (...).")
    return "\n".join(lines)


def trend(first: float, last: float, low: float, high: float) -> str:
    spread = high - low
    if spread == 0 or spread < 0.05 * max(abs(high), abs(low), 1e-9):
        return "flat"
    if last - first > 0.5 * spread:
        return "rising"
    if first - last > 0.5 * spread:
        return "falling"
    return "fluctuating"


def matrix(result: list[dict]) -> str:
    """Per series: a summary (first, last, min, max, trend) and at most MAX_POINTS sampled points."""
    if not result:
        return "No series matched in this time range."
    blocks = []
    for s in result[:MAX_SERIES]:
        points = [(float(t), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
        if not points:
            blocks.append(f"{labels(s['metric'])}: no numeric values")
            continue
        values = [v for _, v in points]
        first, last, low, high = values[0], values[-1], min(values), max(values)
        stride = max(1, -(-len(points) // MAX_POINTS))  # ceiling division
        sampled = points[::stride]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        t0 = points[0][0]
        series_points = ", ".join(f"+{int((t - t0) // 60)}m {number(str(v))}" for t, v in sampled)
        blocks.append(
            f"{labels(s['metric'])}: first {number(str(first))}, last {number(str(last))}, "
            f"min {number(str(low))}, max {number(str(high))}, trend {trend(first, last, low, high)}\n"
            f"  points: {series_points}"
        )
    if len(result) > MAX_SERIES:
        blocks.append(f"... {len(result) - MAX_SERIES} more series not shown. Narrow the query.")
    return "\n".join(blocks)
