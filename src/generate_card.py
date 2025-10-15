from __future__ import annotations

import argparse
import logging
import os
import sys
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


class FriendlyArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with hints for common mistakes."""

    def error(self, message: str) -> None:  # noqa: D401 - keep argparse signature
        if "argument -m/--message" in message and "expected one argument" in message:
            message += (
                "\nПодсказка: если хотите передать текст прямо в команду, "
                "заключите его в кавычки или разместите флаг -m перед остальными опциями. "
                "Например: python -m src.generate_card -o output/card.jpg -m \"-15:00 ...\"."
            )
        super().error(message)

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

    scores = []
    if traffic_client is None:
        api_key = os.environ.get("YANDEX_TRAFFIC_API_KEY")
        if api_key:
            traffic_client = YandexTrafficClient(TrafficConfig(api_key=api_key, directions=DEFAULT_DIRECTIONS))
        else:
            LOGGER.warning(
                "Traffic key not provided; traffic data will be skipped and default scores will be used."
            )

    if traffic_client is not None:
        try:
            scores = traffic_client.fetch_scores()
        except Exception as exc:  # pragma: no cover - defensive path
            LOGGER.warning("Не удалось получить данные пробок: %s", exc)
            scores = []

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


def _read_message(source: str | os.PathLike[str] | None) -> str:
    if source is None:
        text = os.sys.stdin.read()
        if not text.strip():
            raise RuntimeError(
                "Текст сообщения не передан. Укажите файл через --message или вставьте текст в стандартный ввод."
            )
        return text

    raw_value = str(source)
    if raw_value == "-":
        text = os.sys.stdin.read()
        if not text.strip():
            raise RuntimeError(
                "Текст сообщения не передан. Укажите файл через --message или вставьте текст в стандартный ввод."
            )
        return text

    candidate_path = Path(raw_value).expanduser()
    search_paths = [candidate_path]
    if not candidate_path.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent
        search_paths.append(repo_root / candidate_path)

    for path_option in search_paths:
        if path_option.exists():
            text = path_option.read_text(encoding="utf-8")
            if not text.strip():
                raise RuntimeError(f"Файл {path_option} пуст. Добавьте текст сообщения о мосте.")
            return text

    inline_candidate = raw_value.strip()
    if inline_candidate.startswith("-") and inline_candidate != "-":
        return inline_candidate

    if any(separator in inline_candidate for separator in ("\n", "\r")) or " " in inline_candidate:
        if not inline_candidate:
            raise RuntimeError("Текст сообщения не передан. Добавьте описание ситуации на мосту.")
        return inline_candidate

    resolved_display = ", ".join(str(p.resolve()) for p in search_paths)
    raise RuntimeError(
        "Файл с сообщением не найден. Проверьте путь (проверены: "
        f"{resolved_display}) или укажите '-' для ввода из терминала."
    )


def main(argv: Optional[list[str]] = None) -> None:
    parser = FriendlyArgumentParser(
        description="Generate a Crimean Bridge situation card",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--message",
        "-m",
        help="Путь к файлу с сообщением, '-' для чтения из stdin или сам текст (если содержит пробелы)",
        default=None,
    )
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

    def _merge_message_arguments(raw_args: list[str]) -> list[str]:
        if not raw_args:
            return raw_args

        message_flags = {"-m", "--message"}
        known_flags = {
            "-m",
            "--message",
            "-o",
            "--output",
            "--background",
            "--traffic-key",
            "--log-level",
            "--figma-layer-config",
        }

        merged: list[str] = []
        i = 0
        while i < len(raw_args):
            token = raw_args[i]
            if token in message_flags:
                merged.append(token)
                i += 1
                value_tokens: list[str] = []
                while i < len(raw_args):
                    current = raw_args[i]
                    current_key = current.split("=", 1)[0]
                    if current == "--":
                        i += 1
                        break
                    if current_key in known_flags and value_tokens:
                        break
                    if current_key in known_flags and not value_tokens:
                        break
                    value_tokens.append(current)
                    i += 1
                if value_tokens:
                    merged.append(" ".join(value_tokens))
                continue

            merged.append(token)
            i += 1

        return merged

    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_merge_message_arguments(raw_argv))
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
