"""Generate the p95 comparison chart from the documented local benchmark runs."""

from __future__ import annotations

import math
from pathlib import Path


TARGET_RATES = (500, 600, 700)
BASELINE_P95_MS = (33, 3929, 10000)
KAFKA_P95_MS = (8.3, 6.1, 5.8)
WIDTH = 880
HEIGHT = 500
PLOT_LEFT = 120
PLOT_RIGHT = 790
PLOT_TOP = 85
PLOT_BOTTOM = 390


def chart_y(value: float, minimum: float, maximum: float) -> float:
    fraction = (math.log(value) - math.log(minimum)) / (
        math.log(maximum) - math.log(minimum)
    )
    return PLOT_BOTTOM - fraction * (PLOT_BOTTOM - PLOT_TOP)


def render_chart() -> str:
    values = BASELINE_P95_MS + KAFKA_P95_MS
    minimum = min(values)
    maximum = max(values)
    x_positions = tuple(
        PLOT_LEFT + index * (PLOT_RIGHT - PLOT_LEFT) / (len(TARGET_RATES) - 1)
        for index in range(len(TARGET_RATES))
    )
    baseline_points = tuple(
        (x, chart_y(value, minimum, maximum))
        for x, value in zip(x_positions, BASELINE_P95_MS)
    )
    kafka_points = tuple(
        (x, chart_y(value, minimum, maximum))
        for x, value in zip(x_positions, KAFKA_P95_MS)
    )

    def point_list(points: tuple[tuple[float, float], ...]) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<style>",
        "text{font-family:Arial,sans-serif;fill:#1f2937}",
        ".title{font-size:23px;font-weight:700}",
        ".axis-label{font-size:16px}",
        ".tick,.legend,.value{font-size:14px}",
        ".baseline{stroke:#b42318;fill:none}",
        ".kafka{stroke:#175cd3;fill:none}",
        ".grid{stroke:#d0d5dd;stroke-width:1}",
        "</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="440" y="38" text-anchor="middle" class="title">'
        "p95 latency: PostgreSQL baseline vs Kafka</text>",
        f'<line x1="{PLOT_LEFT}" y1="{PLOT_TOP}" x2="{PLOT_LEFT}" '
        f'y2="{PLOT_BOTTOM}" class="grid"/>',
        f'<line x1="{PLOT_LEFT}" y1="{PLOT_BOTTOM}" x2="{PLOT_RIGHT}" '
        f'y2="{PLOT_BOTTOM}" class="grid"/>',
    ]

    for x, rate in zip(x_positions, TARGET_RATES):
        elements.append(
            f'<line x1="{x:.1f}" y1="{PLOT_BOTTOM}" x2="{x:.1f}" '
            f'y2="{PLOT_BOTTOM + 6}" class="grid"/>'
        )
        elements.append(
            f'<text x="{x:.1f}" y="{PLOT_BOTTOM + 27}" '
            f'text-anchor="middle" class="tick">{rate}</text>'
        )

    elements.extend(
        [
            f'<text x="{(PLOT_LEFT + PLOT_RIGHT) / 2:.1f}" y="465" '
            'text-anchor="middle" class="axis-label">Target rate (pings/s)</text>',
            '<text x="28" y="245" text-anchor="middle" class="axis-label" '
            'transform="rotate(-90 28 245)">'
            "p95 latency (ms, logarithmic scale)</text>",
            f'<polyline points="{point_list(baseline_points)}" class="baseline" stroke-width="3"/>',
            f'<polyline points="{point_list(kafka_points)}" class="kafka" stroke-width="3"/>',
        ]
    )

    for (x, y), rate, value in zip(baseline_points, TARGET_RATES, BASELINE_P95_MS):
        elements.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#b42318"/>'
        )
        label = "10,000 ms (timeout)" if value == 10000 else f"{value:,} ms"
        label_x = x - 10 if rate == TARGET_RATES[-1] else x + 10
        anchor = ' text-anchor="end"' if rate == TARGET_RATES[-1] else ""
        elements.append(
            f'<text x="{label_x:.1f}" y="{y - 10:.1f}" '
            f'class="value"{anchor}>{label}</text>'
        )

    for (x, y), rate, value in zip(kafka_points, TARGET_RATES, KAFKA_P95_MS):
        elements.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#175cd3"/>'
        )
        label_x = x - 10 if rate == TARGET_RATES[-1] else x + 10
        anchor = ' text-anchor="end"' if rate == TARGET_RATES[-1] else ""
        elements.append(
            f'<text x="{label_x:.1f}" y="{y - 10:.1f}" '
            f'class="value"{anchor}>{value:g} ms</text>'
        )

    elements.extend(
        [
            '<line x1="150" y1="65" x2="180" y2="65" class="baseline" stroke-width="3"/>',
            '<text x="188" y="70" class="legend">PostgreSQL baseline</text>',
            '<line x1="150" y1="88" x2="180" y2="88" class="kafka" stroke-width="3"/>',
            '<text x="188" y="93" class="legend">Kafka</text>',
            "</svg>",
            "",
        ]
    )
    return "\n".join(elements)


def main() -> None:
    output = Path(__file__).parent / "img" / "p95-latency-vs-target-rate.svg"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_chart(), encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
