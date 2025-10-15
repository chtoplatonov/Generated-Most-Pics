from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from io import BytesIO

from PIL import Image

from .data_providers.traffic import DirectionConfig, TrafficConfig, YandexTrafficClient
from .data_providers.weather import WeatherClient
from .message_parser import parse_bridge_message
from .models import CardData, TrafficScore
from .figma_card_renderer import FigmaCardRenderer


LOGGER = logging.getLogger(__name__)

DEFAULT_BACKGROUND = Path("assets/backgrounds/placeholder.jpg")
DEFAULT_DIRECTIONS = (
    DirectionConfig(name="в Крым", longitude=36.5134, latitude=45.3612, radius=2500),
    DirectionConfig(name="на Кубань", longitude=36.5165, latitude=45.3013, radius=2500),
)


def build_card_data(
    message: str,
    *,
    background: str | os.PathLike[str] | None = None,
    weather_client: WeatherClient | None = None,
    traffic_client: YandexTrafficClient | None = None,
) -> CardData:
    report = parse_bridge_message(message)
    tzinfo = report.time.tzinfo
    generated_at = datetime.now(tzinfo)

    weather_client = weather_client or WeatherClient()
    weather = weather_client.fetch()

    if traffic_client is None:
        api_key = os.environ.get("YANDEX_TRAFFIC_API_KEY")
        if not api_key:
            raise RuntimeError("YANDEX_TRAFFIC_API_KEY environment variable is required for traffic data")
        traffic_client = YandexTrafficClient(TrafficConfig(api_key=api_key, directions=DEFAULT_DIRECTIONS))

    scores = traffic_client.fetch_scores()
    score_map = {score.direction: score for score in scores}
    traffic_to_crimea = score_map.get("в Крым") or TrafficScore(direction="в Крым", score=0)
    traffic_to_kuban = score_map.get("на Кубань") or TrafficScore(direction="на Кубань", score=0)

    card_data = CardData(
        report=report,
        generated_at=generated_at,
        weather=weather,
        traffic_to_crimea=traffic_to_crimea,
        traffic_to_kuban=traffic_to_kuban,
        background_path=str(background or DEFAULT_BACKGROUND),
    )
    return card_data


def generate_card_image(
    card_data: CardData,
    *,
    figma_renderer: FigmaCardRenderer | None = None,
    figma_layer_config: str | None = None,
) -> Image.Image:
    renderer = figma_renderer or FigmaCardRenderer.from_env(layer_config_path=figma_layer_config)
    image_bytes = renderer.render_card(card_data)
    return Image.open(BytesIO(image_bytes))


def _read_message(path: str | os.PathLike[str] | None) -> str:
    if path is None or str(path) == "-":
        text = os.sys.stdin.read()
        if not text.strip():
            raise RuntimeError(
                "Текст сообщения не передан. Укажите файл через --message или вставьте текст в стандартный ввод."
            )
        return text

    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Файл с сообщением {file_path} не найден. Проверьте путь или укажите '-' для ввода из терминала."
        ) from exc

    if not text.strip():
        raise RuntimeError(f"Файл {file_path} пуст. Добавьте текст сообщения о мосте.")

    return text


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Generate a Crimean Bridge situation card")
    parser.add_argument("--message", "-m", help="Path to the message file or '-' to read from stdin", default="-")
    parser.add_argument("--output", "-o", help="Path to save the generated image", default="card.png")
    parser.add_argument("--background", help="Optional path to a background image", default=None)
    parser.add_argument(
        "--traffic-key",
        dest="traffic_key",
        help="Yandex Maps API key (overrides YANDEX_TRAFFIC_API_KEY)",
        default=None,
    )
    parser.add_argument("--log-level", help="Logging level", default="INFO")
    parser.add_argument(
        "--figma-layer-config",
        help="Path to JSON overrides for mapping card data to Figma layers",
        default=None,
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    try:
        message_text = _read_message(args.message)
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc

    weather_client = WeatherClient()

    traffic_client: YandexTrafficClient | None = None
    traffic_key = args.traffic_key or os.environ.get("YANDEX_TRAFFIC_API_KEY")
    if traffic_key:
        traffic_client = YandexTrafficClient(TrafficConfig(api_key=traffic_key, directions=DEFAULT_DIRECTIONS))
    else:
        LOGGER.warning("Traffic key not provided; traffic fetch will fail")

    card_data = build_card_data(
        message_text,
        background=args.background,
        weather_client=weather_client,
        traffic_client=traffic_client,
    )
    card = generate_card_image(card_data, figma_layer_config=args.figma_layer_config)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    card.save(output_path, format=output_path.suffix.replace(".", "").upper() or "PNG")
    LOGGER.info("Card saved to %s", output_path)


if __name__ == "__main__":
    main()
