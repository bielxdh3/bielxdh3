#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 900, 320
LEFT, RIGHT, TOP, BOTTOM = 92, 40, 90, 56
PLOT_W = WIDTH - LEFT - RIGHT
PLOT_H = HEIGHT - TOP - BOTTOM
BG = "#0D1117"
BORDER = "#30363D"
GRID = "#27303A"
AXIS = "#8B949E"
TEXT = "#E6EDF3"
CYAN = "#B8E6FF"
MID = "#9ED8FA"
PURPLE = "#C8B6FF"
AREA = "#273646"


def format_pt(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def font(size: int, *, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


FONT_TITLE = font(22, bold=True)
FONT_VALUE = font(22, bold=True)
FONT_SUB = font(12)
FONT_AXIS = font(11)
FONT_SMALL = font(10)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix_rgb(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(lerp(x, y, t)) for x, y in zip(a, b))


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def load_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    series = payload.get("series") or []
    if not series:
        raise RuntimeError("commit-history.json não contém série para animar.")
    if int(series[-1]["total"]) != int(payload.get("total_contributions", -1)):
        raise RuntimeError("O total final da série diverge de total_contributions.")
    return payload


def chart_geometry(series: list[dict], total: int):
    y_max = max(10, total + max(5, math.ceil(total * 0.08)))

    def x_for(position: float) -> float:
        if len(series) <= 1:
            return LEFT + PLOT_W / 2
        return LEFT + (position / (len(series) - 1)) * PLOT_W

    def y_for(value: float) -> float:
        return TOP + (1 - (value / y_max)) * PLOT_H

    return y_max, x_for, y_for


def text_right(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str, font_obj, fill: str):
    x, y = xy
    box = draw.textbbox((0, 0), value, font=font_obj)
    draw.text((x - (box[2] - box[0]), y), value, font=font_obj, fill=fill)


def render_frame(payload: dict, progress: float) -> Image.Image:
    login = str(payload.get("user", "bielxdh3"))
    total = int(payload["total_contributions"])
    include_private = bool(payload.get("includes_private_contributions"))
    series = payload["series"]
    y_max, x_for, y_for = chart_geometry(series, total)

    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (1, 1, WIDTH - 2, HEIGHT - 2),
        radius=18,
        fill=BG,
        outline=BORDER,
        width=1,
    )

    first_day = date.fromisoformat(str(series[0]["date"]))
    private_note = (
        "repositórios privados permanecem anônimos"
        if include_private
        else "somente contribuições públicas visíveis"
    )

    draw.text((42, 18), "EVOLUÇÃO DAS CONTRIBUIÇÕES", font=FONT_TITLE, fill=TEXT)
    draw.text(
        (42, 49),
        f"desde {first_day.strftime('%d/%m/%Y')} · {private_note}",
        font=FONT_SUB,
        fill=AXIS,
    )
    text_right(draw, (858, 18), "0", FONT_VALUE, CYAN)
    text_right(draw, (858, 49), f"@{login}", FONT_SMALL, AXIS)

    for i in range(5):
        ratio = i / 4
        value = round(y_max - ratio * y_max)
        y = TOP + ratio * PLOT_H
        draw.line((LEFT, y, LEFT + PLOT_W, y), fill=GRID, width=1)
        label = format_pt(value)
        box = draw.textbbox((0, 0), label, font=FONT_AXIS)
        draw.text(
            (LEFT - 14 - (box[2] - box[0]), y - 5),
            label,
            font=FONT_AXIS,
            fill=AXIS,
        )

    draw.line(
        (LEFT, TOP + PLOT_H, LEFT + PLOT_W, TOP + PLOT_H),
        fill="#3A4550",
        width=1,
    )

    used = set()
    for ratio in (0, 0.25, 0.5, 0.75, 1):
        idx = round((len(series) - 1) * ratio)
        if idx in used:
            continue
        used.add(idx)
        day = date.fromisoformat(str(series[idx]["date"]))
        label = day.strftime("%d/%m/%y")
        x = x_for(idx)
        box = draw.textbbox((0, 0), label, font=FONT_AXIS)
        draw.text(
            (x - (box[2] - box[0]) / 2, TOP + PLOT_H + 29),
            label,
            font=FONT_AXIS,
            fill=AXIS,
        )

    if progress <= 0:
        return image

    pos = progress * (len(series) - 1)
    full_idx = int(math.floor(pos))
    frac = pos - full_idx
    points: list[tuple[float, float]] = []

    for idx in range(full_idx + 1):
        value = float(series[idx]["total"])
        points.append((x_for(idx), y_for(value)))

    current_total = float(series[full_idx]["total"])
    if full_idx < len(series) - 1 and frac > 0:
        a = float(series[full_idx]["total"])
        b = float(series[full_idx + 1]["total"])
        current_total = lerp(a, b, frac)
        points.append((x_for(pos), y_for(current_total)))

    if points:
        polygon = [
            (points[0][0], TOP + PLOT_H),
            *points,
            (points[-1][0], TOP + PLOT_H),
        ]
        draw.polygon(polygon, fill=AREA)

    c0, c1, c2 = hex_rgb(CYAN), hex_rgb(MID), hex_rgb(PURPLE)
    for i in range(1, len(points)):
        t = i / max(1, len(points) - 1)
        color = (
            mix_rgb(c0, c1, t / 0.55)
            if t <= 0.55
            else mix_rgb(c1, c2, (t - 0.55) / 0.45)
        )
        draw.line((points[i - 1], points[i]), fill=color, width=4)

    if points:
        x, y = points[-1]
        draw.ellipse(
            (x - 6, y - 6, x + 6, y + 6),
            fill=PURPLE,
            outline=CYAN,
            width=3,
        )

    draw.rectangle((700, 14, 860, 44), fill=BG)
    text_right(draw, (858, 18), format_pt(round(current_total)), FONT_VALUE, CYAN)
    return image


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera GIF animado do histórico de contribuições do perfil."
    )
    parser.add_argument("--data", default="data/commit-history.json")
    parser.add_argument("--output", default="assets/commit-history.gif")
    parser.add_argument("--frames", type=int, default=56)
    parser.add_argument("--frame-ms", type=int, default=45)
    parser.add_argument("--intro-ms", type=int, default=700)
    parser.add_argument("--hold-ms", type=int, default=1800)
    args = parser.parse_args()

    if args.frames < 12:
        raise SystemExit("--frames precisa ser pelo menos 12.")

    payload = load_payload(Path(args.data))
    # O primeiro frame é deliberadamente o estado atual completo. Isso faz com
    # que previews estáticos, carregamento inicial e caches do GitHub mostrem o
    # valor correto em vez de "0" antes da animação começar.
    frames = [render_frame(payload, 1.0)]
    for i in range(args.frames):
        linear = i / (args.frames - 1)
        frames.append(render_frame(payload, ease_in_out_cubic(linear)))

    palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    indexed = [
        frame.quantize(palette=palette, dither=Image.Dither.NONE)
        for frame in frames
    ]
    durations = [args.intro_ms] + [args.frame_ms] * args.frames
    durations[-1] = args.hold_ms

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    indexed[0].save(
        output,
        save_all=True,
        append_images=indexed[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(
        f"GIF atualizado: {output} "
        f"({output.stat().st_size / 1024:.1f} KiB, {len(indexed)} frames; "
        f"primeiro frame = estado atual)"
    )


if __name__ == "__main__":
    main()
