from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

from .models import CardData, TrafficScore


CARD_SIZE = (1280, 960)
PANEL_COUNT = 4
PANEL_RADIUS = 50
PANEL_FILL = (255, 255, 255, 180)
PANEL_BORDER = (255, 255, 255, 230)
ACCENT_COLOR = (35, 70, 120)
DEFAULT_FONT = "DejaVuSans.ttf"
DEFAULT_FONT_BOLD = "DejaVuSans-Bold.ttf"


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default()


def _load_background(path: str | os.PathLike[str] | None) -> Image.Image:
    if path:
        file = Path(path)
        if file.exists():
            return Image.open(file).convert("RGBA")
    # Fallback: create a simple gradient background reminiscent of the sea.
    width, height = CARD_SIZE
    gradient = Image.new("RGBA", CARD_SIZE, (44, 117, 255, 255))
    draw = ImageDraw.Draw(gradient)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(30 + 20 * ratio)
        g = int(90 + 30 * ratio)
        b = int(160 + 80 * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
    return gradient


def _format_date(dt: datetime) -> str:
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    weekday = weekdays[dt.weekday()]
    return f"{weekday} {dt:%d.%m}"


def _weather_category(code: int) -> str:
    if code in {0, 1}:
        return "clear"
    if code == 2:
        return "partly"
    if code in {3, 45, 48}:
        return "cloudy"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 80, 81, 82}:
        return "rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "thunder"
    return "cloudy"


def _draw_panel(draw: ImageDraw.ImageDraw, rect: Tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(rect, radius=PANEL_RADIUS, fill=PANEL_FILL, outline=PANEL_BORDER, width=2)


def _create_weather_icon(category: str, size: int) -> Image.Image:
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    center = size // 2
    if category == "clear":
        radius = size * 0.32
        draw.ellipse(
            [center - radius, center - radius, center + radius, center + radius],
            fill=(255, 210, 77, 255),
        )
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x0 = center + math.cos(rad) * radius * 1.5
            y0 = center + math.sin(rad) * radius * 1.5
            x1 = center + math.cos(rad) * radius * 2.2
            y1 = center + math.sin(rad) * radius * 2.2
            draw.line([(x0, y0), (x1, y1)], fill=(255, 210, 77, 255), width=max(2, size // 18))
    else:
        cloud_color = (255, 255, 255, 230)
        offsets = [(-0.25, 0.05), (0.2, -0.1), (0.5, 0.1)]
        base_radius = size * 0.28
        for dx, dy in offsets:
            cx = center + dx * size
            cy = center + dy * size
            draw.ellipse(
                [cx - base_radius, cy - base_radius, cx + base_radius, cy + base_radius],
                fill=cloud_color,
            )
        draw.rectangle(
            [center - base_radius * 1.4, center, center + base_radius * 1.4, center + base_radius],
            fill=cloud_color,
        )
        if category == "partly":
            sun_radius = size * 0.22
            sun_center = (center - size * 0.25, center - size * 0.35)
            draw.ellipse(
                [sun_center[0] - sun_radius, sun_center[1] - sun_radius, sun_center[0] + sun_radius, sun_center[1] + sun_radius],
                fill=(255, 210, 77, 255),
            )
        elif category == "rain":
            drop_color = (70, 130, 180, 255)
            for offset in (-0.35, 0, 0.35):
                x = center + offset * size * 0.4
                draw.line(
                    [(x, center + size * 0.2), (x, center + size * 0.45)],
                    fill=drop_color,
                    width=max(2, size // 18),
                )
        elif category == "snow":
            flake_color = (200, 230, 255, 255)
            for offset in (-0.3, 0.3):
                x = center + offset * size * 0.35
                y = center + size * 0.25
                draw.line([(x - 8, y), (x + 8, y)], fill=flake_color, width=2)
                draw.line([(x, y - 8), (x, y + 8)], fill=flake_color, width=2)
                draw.line([(x - 6, y - 6), (x + 6, y + 6)], fill=flake_color, width=2)
                draw.line([(x - 6, y + 6), (x + 6, y - 6)], fill=flake_color, width=2)
        elif category == "thunder":
            bolt = [
                (center - size * 0.12, center + size * 0.05),
                (center, center + size * 0.05),
                (center - size * 0.08, center + size * 0.35),
                (center + size * 0.1, center + size * 0.35),
                (center - size * 0.05, center + size * 0.65),
            ]
            draw.polygon(bolt, fill=(255, 215, 0, 255))
        elif category == "fog":
            fog_color = (220, 220, 220, 255)
            for i in range(3):
                y = center + size * 0.2 + i * size * 0.12
                draw.line([(center - size * 0.5, y), (center + size * 0.5, y)], fill=fog_color, width=max(2, size // 16))
    return icon


def _score_color(score: int) -> Tuple[int, int, int]:
    if score <= 3:
        return 57, 193, 108
    if score <= 6:
        return 248, 193, 80
    return 235, 87, 87


def _draw_score_panel(
    draw: ImageDraw.ImageDraw,
    rect: Tuple[int, int, int, int],
    traffic_score: TrafficScore,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
) -> None:
    _draw_panel(draw, rect)
    x0, y0, x1, y1 = rect
    width = x1 - x0
    height = y1 - y0
    circle_diameter = int(min(width, height) * 0.55)
    circle_x = x0 + (width - circle_diameter) // 2
    circle_y = y0 + int(height * 0.12)
    circle_bounds = (circle_x, circle_y, circle_x + circle_diameter, circle_y + circle_diameter)
    circle_color = _score_color(traffic_score.score)

    draw.ellipse(circle_bounds, fill=(*circle_color, 255))

    value_text = str(int(round(traffic_score.score)))
    value_w, value_h = draw.textsize(value_text, font=value_font)
    value_x = x0 + width // 2 - value_w // 2
    value_y = circle_y + circle_diameter // 2 - value_h // 2
    draw.text((value_x, value_y), value_text, font=value_font, fill=(255, 255, 255, 255))

    label_text = traffic_score.direction
    label_w, label_h = draw.textsize(label_text, font=label_font)
    label_x = x0 + width // 2 - label_w // 2
    label_y = circle_y + circle_diameter + (height - (circle_y + circle_diameter - y0)) / 2 - label_h / 2
    draw.text((label_x, label_y), label_text, font=label_font, fill=(255, 255, 255, 255))


def render_card(data: CardData) -> Image.Image:
    base = _load_background(data.background_path).convert("RGBA")
    base = base.resize(CARD_SIZE, Image.LANCZOS)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    title_font = _load_font(DEFAULT_FONT_BOLD, 60)
    subtitle_font = _load_font(DEFAULT_FONT, 48)
    medium_font = _load_font(DEFAULT_FONT_BOLD, 72)
    weather_font = _load_font(DEFAULT_FONT_BOLD, 88)

    panel_width = 260
    panel_height = 240
    gap = 30
    total_width = PANEL_COUNT * panel_width + (PANEL_COUNT - 1) * gap
    start_x = (CARD_SIZE[0] - total_width) // 2
    top_y = 90

    for index in range(PANEL_COUNT):
        x = start_x + index * (panel_width + gap)
        y = top_y
        rect = (x, y, x + panel_width, y + panel_height)
        _draw_panel(draw, rect)

    # Date/time panel
    first_rect = (start_x, top_y, start_x + panel_width, top_y + panel_height)
    date_text = _format_date(data.generated_at)
    time_text = f"{data.report.time:%H:%M}"
    draw.text(
        (first_rect[0] + 40, first_rect[1] + 50),
        date_text,
        font=title_font,
        fill=ACCENT_COLOR,
    )
    draw.text(
        (first_rect[0] + 40, first_rect[1] + 140),
        time_text,
        font=weather_font,
        fill=ACCENT_COLOR,
    )

    # Weather panel
    second_rect = (
        start_x + (panel_width + gap),
        top_y,
        start_x + (panel_width + gap) + panel_width,
        top_y + panel_height,
    )
    category = _weather_category(data.weather.condition_code)
    icon = _create_weather_icon(category, 150)
    icon_x = int(second_rect[0] + panel_width * 0.18)
    icon_y = int(second_rect[1] + panel_height * 0.18)
    overlay.alpha_composite(icon, dest=(icon_x, icon_y))
    temp_text = f"{round(data.weather.temperature_c):d}°"
    temp_x = int(second_rect[0] + panel_width * 0.55)
    temp_y = int(second_rect[1] + panel_height * 0.32)
    draw.text((temp_x, temp_y), temp_text, font=weather_font, fill=ACCENT_COLOR)

    # Traffic panels
    third_rect = (
        start_x + 2 * (panel_width + gap),
        top_y,
        start_x + 2 * (panel_width + gap) + panel_width,
        top_y + panel_height,
    )
    fourth_rect = (
        start_x + 3 * (panel_width + gap),
        top_y,
        start_x + 3 * (panel_width + gap) + panel_width,
        top_y + panel_height,
    )

    _draw_score_panel(draw, third_rect, data.traffic_to_crimea, subtitle_font, medium_font)
    _draw_score_panel(draw, fourth_rect, data.traffic_to_kuban, subtitle_font, medium_font)

    composed = Image.alpha_composite(base, overlay)
    return composed.convert("RGB")
