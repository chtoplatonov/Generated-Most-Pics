from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .figma_card_renderer import FigmaCardRenderer
from .generate_card import build_card_data, generate_card_image
from .data_providers.weather import WeatherClient
from .data_providers.traffic import TrafficConfig, YandexTrafficClient, DirectionConfig


LOGGER = logging.getLogger(__name__)


class BridgeCardBot:
    """Telegram bot that listens for bridge updates and posts generated cards."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        bot_token: str,
        source_chat: str | int,
        destination_chat: Optional[str | int] = None,
        destination_thread_id: Optional[int] = None,
        session_name: str = "bridge_card_bot",
        background: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        self.source_chat = source_chat
        self.destination_chat = destination_chat or source_chat
        self.destination_thread_id = destination_thread_id
        self.background = background
        self.weather_client = WeatherClient()
        self.figma_renderer = FigmaCardRenderer.from_env()

        traffic_key = os.environ.get("YANDEX_TRAFFIC_API_KEY")
        if not traffic_key:
            raise RuntimeError("YANDEX_TRAFFIC_API_KEY must be set for the bot to fetch traffic data")
        directions = (
            DirectionConfig(name="в Крым", longitude=36.5134, latitude=45.3612, radius=2500),
            DirectionConfig(name="на Кубань", longitude=36.5165, latitude=45.3013, radius=2500),
        )
        self.traffic_client = YandexTrafficClient(TrafficConfig(api_key=traffic_key, directions=directions))

        session_dir = Path(__file__).resolve().parent.parent / ".telegram_session"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 - хотим дать понятную подсказку
            raise RuntimeError(
                "Не удалось подготовить папку для сессии Telegram. Проверьте права доступа и путь к проекту."
            ) from exc

        self.client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir=str(session_dir),
        )
        self.client.add_handler(MessageHandler(self._on_message, filters.chat(self.source_chat)))

    async def _on_message(self, client: Client, message: Message) -> None:
        if not message.text:
            LOGGER.debug("Ignoring message without text: %s", message.id)
            return

        LOGGER.info("Processing message %s", message.id)
        try:
            card_data = build_card_data(
                message.text,
                background=self.background,
                weather_client=self.weather_client,
                traffic_client=self.traffic_client,
            )
            image = generate_card_image(card_data, figma_renderer=self.figma_renderer)
        except Exception as exc:  # noqa: BLE001 - top-level handler to notify chat
            LOGGER.exception("Failed to generate card: %s", exc)
            await client.send_message(self.destination_chat, f"Не удалось создать карточку: {exc}")
            return

        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        buffer.seek(0)
        caption = f"{card_data.report.time:%H:%M} — обновление по Крымскому мосту"
        await client.send_photo(
            self.destination_chat,
            photo=buffer,
            caption=caption,
            message_thread_id=self.destination_thread_id,
        )
        LOGGER.info("Card published for message %s", message.id)

    async def run(self) -> None:
        async with self.client:
            LOGGER.info("BridgeCardBot is running. Listening on %s", self.source_chat)
            await asyncio.Future()


def run_bot_from_env() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def _load_dotenv(path: Path = Path(".env")) -> None:
        if path.exists():
            try:
                for raw_line in path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    cleaned_key = key.strip()
                    cleaned_value = value.strip()
                    if cleaned_value and cleaned_value[0] == cleaned_value[-1] and cleaned_value[0] in {'"', "'"}:
                        cleaned_value = cleaned_value[1:-1]
                    os.environ.setdefault(cleaned_key, cleaned_value)
                LOGGER.info("Значения окружения загружены из %s", path)
            except OSError as exc:
                LOGGER.warning("Не удалось прочитать %s: %s", path, exc)

    _load_dotenv()

    def _require_env(name: str) -> str:
        value = os.environ.get(name)
        if value:
            cleaned = value.strip()
            if cleaned:
                return cleaned
        raise RuntimeError(
            f"Не найдено значение переменной окружения {name}. Заполните её через .env или экспорт и повторите запуск."
        )

    api_id_raw = _require_env("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:  # noqa: BLE001 - сразу подсказываем корректный формат
        raise RuntimeError("TELEGRAM_API_ID должно быть целым числом, как выдали на my.telegram.org") from exc

    api_hash = _require_env("TELEGRAM_API_HASH")
    bot_token = _require_env("TELEGRAM_BOT_TOKEN")
    source_chat_raw = os.environ.get("TELEGRAM_SOURCE_CHAT")
    source_chat = source_chat_raw.strip() if source_chat_raw else None
    if not source_chat:
        raise RuntimeError("Нужно указать TELEGRAM_SOURCE_CHAT — канал, где бот читает сообщения.")

    destination_chat_raw = os.environ.get("TELEGRAM_DESTINATION_CHAT")
    destination_chat = destination_chat_raw.strip() if destination_chat_raw else source_chat
    destination_thread_id_raw = os.environ.get("TELEGRAM_DESTINATION_THREAD_ID")
    if destination_thread_id_raw:
        cleaned_thread_id = destination_thread_id_raw.strip()
        if not cleaned_thread_id:
            destination_thread_id = None
        else:
            try:
                destination_thread_id = int(cleaned_thread_id)
            except ValueError as exc:  # noqa: BLE001 - объясняем корректный формат
                raise RuntimeError(
                    "TELEGRAM_DESTINATION_THREAD_ID должно быть числом — используйте ID ветки из Telegram."
                ) from exc
    else:
        destination_thread_id = None
    background_raw = os.environ.get("CARD_BACKGROUND")
    background = background_raw.strip() if background_raw else None

    bot = BridgeCardBot(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        source_chat=source_chat,
        destination_chat=destination_chat,
        destination_thread_id=destination_thread_id,
        background=background,
    )
    asyncio.run(bot.run())


__all__ = ["BridgeCardBot", "run_bot_from_env"]


if __name__ == "__main__":
    run_bot_from_env()
