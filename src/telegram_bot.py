from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Optional

from urllib.parse import urlparse

from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .env_loader import load_dotenv
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

        raw_traffic_key = os.environ.get("YANDEX_TRAFFIC_API_KEY")
        cleaned_traffic_key = raw_traffic_key.strip() if raw_traffic_key and raw_traffic_key.strip() else None
        directions = (
            DirectionConfig(name="в Крым", longitude=36.5134, latitude=45.3612, radius=2500),
            DirectionConfig(name="на Кубань", longitude=36.5165, latitude=45.3013, radius=2500),
        )
        self.traffic_client = YandexTrafficClient(
            TrafficConfig(directions=directions, api_key=cleaned_traffic_key)
        )

        session_dir = Path(__file__).resolve().parent.parent / ".telegram_session"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 - хотим дать понятную подсказку
            raise RuntimeError(
                "Не удалось подготовить папку для сессии Telegram. Проверьте права доступа и путь к проекту."
            ) from exc

        message_filter = filters.chat(self.source_chat) & filters.incoming

        self.client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir=str(session_dir),
        )
        self.client.add_handler(MessageHandler(self._on_message, message_filter))

    async def _on_message(self, client: Client, message: Message) -> None:
        message_text = (message.text or message.caption or "").strip()
        if not message_text:
            LOGGER.debug("Ignoring message without readable text: %s", message.id)
            return

        LOGGER.info("Processing message %s", message.id)
        try:
            card_data = build_card_data(
                message_text,
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
            await self._log_chat_context()
            LOGGER.info("BridgeCardBot запущен. Ожидаем сообщения…")
            await idle()
            LOGGER.info("Получен сигнал остановки, завершаем работу…")

    async def _log_chat_context(self) -> None:
        source_label = await self._describe_chat(self.source_chat)
        destination_label = await self._describe_chat(self.destination_chat)
        if self.destination_thread_id is not None:
            destination_label = f"{destination_label} (thread {self.destination_thread_id})"
        LOGGER.info(
            "BridgeCardBot is running. Listening on %s and posting to %s",
            source_label,
            destination_label,
        )

    async def _describe_chat(self, chat_ref: str | int) -> str:
        try:
            chat = await self.client.get_chat(chat_ref)
        except Exception as exc:  # noqa: BLE001 - хотим оставить исходное значение для диагностики
            LOGGER.warning("Не удалось получить данные чата %s: %s", chat_ref, exc)
            return str(chat_ref)

        title = chat.title or chat.first_name or chat.username or str(chat.id)
        return f"{title} (id {chat.id})"


def run_bot_from_env() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    load_dotenv(logger=LOGGER)

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

    def _normalise_chat_reference(raw_value: str) -> str | int:
        cleaned = raw_value.strip()
        if not cleaned:
            raise ValueError("Empty chat reference")

        lower_cleaned = cleaned.lower()
        if lower_cleaned.startswith(("http://", "https://")):
            parsed = urlparse(cleaned)
            candidate = parsed.path.lstrip("/")
            if candidate:
                cleaned = candidate
        elif lower_cleaned.startswith("t.me/"):
            cleaned = cleaned.split("/", 1)[-1]

        cleaned = cleaned.split("?", 1)[0]
        cleaned = cleaned.split("/", 1)[0]

        if cleaned.lstrip("-").isdigit():
            return int(cleaned)

        if not cleaned.startswith("@"):
            cleaned = f"@{cleaned}"
        return cleaned

    source_chat_raw = os.environ.get("TELEGRAM_SOURCE_CHAT")
    source_chat_value = source_chat_raw.strip() if source_chat_raw else None
    if not source_chat_value:
        raise RuntimeError("Нужно указать TELEGRAM_SOURCE_CHAT — канал, где бот читает сообщения.")
    try:
        source_chat = _normalise_chat_reference(source_chat_value)
    except ValueError as exc:  # noqa: BLE001 - подсказываем пользователю
        raise RuntimeError("TELEGRAM_SOURCE_CHAT заполнен некорректно. Укажите @username, ID или ссылку t.me.") from exc

    destination_chat_raw = os.environ.get("TELEGRAM_DESTINATION_CHAT")
    if destination_chat_raw:
        destination_chat_value = destination_chat_raw.strip()
        if destination_chat_value:
            try:
                destination_chat = _normalise_chat_reference(destination_chat_value)
            except ValueError as exc:  # noqa: BLE001
                raise RuntimeError(
                    "TELEGRAM_DESTINATION_CHAT заполнен некорректно. Укажите @username, ID или ссылку t.me."
                ) from exc
        else:
            destination_chat = source_chat
    else:
        destination_chat = source_chat
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
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        LOGGER.info("Остановка по запросу пользователя")


__all__ = ["BridgeCardBot", "run_bot_from_env"]


if __name__ == "__main__":
    run_bot_from_env()
