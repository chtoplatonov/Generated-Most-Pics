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
        session_name: str = "bridge_card_bot",
        background: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        self.source_chat = source_chat
        self.destination_chat = destination_chat or source_chat
        self.background = background
        self.weather_client = WeatherClient()

        traffic_key = os.environ.get("YANDEX_TRAFFIC_API_KEY")
        if not traffic_key:
            raise RuntimeError("YANDEX_TRAFFIC_API_KEY must be set for the bot to fetch traffic data")
        directions = (
            DirectionConfig(name="в Крым", longitude=36.5134, latitude=45.3612, radius=2500),
            DirectionConfig(name="на Кубань", longitude=36.5165, latitude=45.3013, radius=2500),
        )
        self.traffic_client = YandexTrafficClient(TrafficConfig(api_key=traffic_key, directions=directions))

        self.client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir=str(Path(".telegram_session").absolute()),
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
            image = generate_card_image(card_data)
        except Exception as exc:  # noqa: BLE001 - top-level handler to notify chat
            LOGGER.exception("Failed to generate card: %s", exc)
            await client.send_message(self.destination_chat, f"Не удалось создать карточку: {exc}")
            return

        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        buffer.seek(0)
        caption = f"{card_data.report.time:%H:%M} — обновление по Крымскому мосту"
        await client.send_photo(self.destination_chat, photo=buffer, caption=caption)
        LOGGER.info("Card published for message %s", message.id)

    async def run(self) -> None:
        async with self.client:
            LOGGER.info("BridgeCardBot is running. Listening on %s", self.source_chat)
            await asyncio.Future()


def run_bot_from_env() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    source_chat = os.environ.get("TELEGRAM_SOURCE_CHAT")
    destination_chat = os.environ.get("TELEGRAM_DESTINATION_CHAT", source_chat)
    background = os.environ.get("CARD_BACKGROUND")

    if not source_chat:
        raise RuntimeError("TELEGRAM_SOURCE_CHAT environment variable is required")

    bot = BridgeCardBot(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        source_chat=source_chat,
        destination_chat=destination_chat,
        background=background,
    )
    asyncio.run(bot.run())


__all__ = ["BridgeCardBot", "run_bot_from_env"]


if __name__ == "__main__":
    run_bot_from_env()
