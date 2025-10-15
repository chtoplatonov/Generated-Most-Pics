"""Utilities for assembling bridge cards via the Figma REST API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

import json
import time

import requests

from .models import CardData


FIGMA_API_URL = "https://api.figma.com/v1"


TextFactory = Callable[[CardData], str]


def _format_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")


def _format_time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _format_temperature(temp_c: float) -> str:
    return f"{temp_c:+.0f}°C"


def _format_score(score: int) -> str:
    suffix = "баллов" if score % 10 != 1 or score % 100 == 11 else "балл"
    return f"{score} {suffix}"


def _build_default_factories() -> Dict[str, TextFactory]:
    return {
        "Дата": lambda data: _format_date(data.report.time),
        "Время": lambda data: _format_time(data.report.time),
        "Обновлено": lambda data: f"Обновлено в {_format_time(data.generated_at)}",
        "Температура": lambda data: _format_temperature(data.weather.temperature_c),
        "Погодное описание": lambda data: data.weather.condition_description,
        "Баллы в Крым": lambda data: _format_score(data.traffic_to_crimea.score),
        "Баллы на Кубань": lambda data: _format_score(data.traffic_to_kuban.score),
        "Комментарий в Крым": lambda data: data.report.kerch_text or "",
        "Комментарий на Кубань": lambda data: data.report.taman_text or "",
    }


@dataclass
class FigmaLayerConfig:
    """Configuration describing how a Figma layer should be populated."""

    text_factory: TextFactory


@dataclass
class FigmaRenderSettings:
    token: str
    file_key: str
    frame_node_id: str
    branch_id: str | None = None
    text_factories: Dict[str, TextFactory] = field(default_factory=_build_default_factories)


class _TemplateContext(dict):
    """Dictionary that returns an empty string for missing keys."""

    def __missing__(self, key: str) -> str:
        return ""


class FigmaCardRenderer:
    """Renderer that fills a predefined card layout in Figma."""

    def __init__(self, settings: FigmaRenderSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": settings.token})

    @classmethod
    def from_env(
        cls,
        *,
        layer_config_path: str | Path | None = None,
    ) -> "FigmaCardRenderer":
        token = _require_env("FIGMA_TOKEN")
        file_key = _require_env("FIGMA_FILE_KEY")
        frame_node_id = _require_env("FIGMA_CARD_NODE_ID")
        branch_id = _optional_env("FIGMA_BRANCH_ID")
        factories = _build_default_factories()
        template_overrides: Mapping[str, str] = {}
        if layer_config_path:
            template_overrides = _load_template_overrides(layer_config_path)
        elif env_path := _optional_env("FIGMA_LAYER_CONFIG"):
            template_overrides = _load_template_overrides(env_path)

        if template_overrides:
            factories.update(_factories_from_templates(template_overrides))

        settings = FigmaRenderSettings(
            token=token,
            file_key=file_key,
            frame_node_id=frame_node_id,
            branch_id=branch_id,
            text_factories=factories,
        )
        return cls(settings)

    def render_card(self, card_data: CardData, *, poll_timeout: float = 30.0) -> bytes:
        text_values = {name: factory(card_data) for name, factory in self.settings.text_factories.items()}
        node_ids = self._resolve_node_ids(text_values.keys())
        missing = [name for name, node_id in node_ids.items() if node_id is None]
        if missing:
            raise RuntimeError(
                "Не удалось найти слои в макете Figma: " + ", ".join(sorted(missing))
            )
        updates = {node_id: text_values[name] for name, node_id in node_ids.items() if node_id}
        self._update_text_nodes(updates)
        image_url = self._request_image_url()
        if not image_url:
            raise RuntimeError("Figma не вернула ссылку на изображение карточки")
        return self._download_image(image_url, poll_timeout=poll_timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    def _resolve_node_ids(self, layer_names: Iterable[str]) -> Dict[str, str | None]:
        response = self.session.get(
            f"{FIGMA_API_URL}/files/{self.settings.file_key}/nodes",
            params={"ids": self.settings.frame_node_id, **self._branch_param()},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        nodes = payload.get("nodes", {})
        frame = nodes.get(self.settings.frame_node_id, {}).get("document")
        if not frame:
            raise RuntimeError("Не удалось получить фрейм карточки из Figma")
        lookup = {name: None for name in layer_names}
        self._traverse_nodes(frame, lookup)
        return lookup

    def _traverse_nodes(self, node: Mapping[str, object], lookup: MutableMapping[str, str | None]) -> None:
        name = node.get("name") if isinstance(node, Mapping) else None
        node_id = node.get("id") if isinstance(node, Mapping) else None
        if isinstance(name, str) and name in lookup and isinstance(node_id, str):
            lookup[name] = node_id
        children = node.get("children") if isinstance(node, Mapping) else None
        if isinstance(children, Sequence):
            for child in children:
                if isinstance(child, Mapping):
                    self._traverse_nodes(child, lookup)

    def _update_text_nodes(self, updates: Mapping[str, str]) -> None:
        if not updates:
            return
        payload = {
            "nodes": [
                {
                    "node_id": node_id,
                    "document": {"id": node_id, "type": "TEXT", "characters": text},
                }
                for node_id, text in updates.items()
            ]
        }
        payload.update(self._branch_param())
        response = self.session.put(
            f"{FIGMA_API_URL}/files/{self.settings.file_key}/nodes",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()

    def _request_image_url(self) -> str | None:
        params = {"ids": self.settings.frame_node_id, "format": "png"}
        params.update(self._branch_param())
        response = self.session.get(
            f"{FIGMA_API_URL}/images/{self.settings.file_key}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        images = payload.get("images", {})
        return images.get(self.settings.frame_node_id)

    def _download_image(self, url: str, *, poll_timeout: float) -> bytes:
        deadline = time.time() + poll_timeout
        while True:
            response = self.session.get(url, timeout=20)
            if response.status_code == 200 and response.content:
                return response.content
            if time.time() >= deadline:
                response.raise_for_status()
                raise RuntimeError("Figma не вернула изображение в отведённое время")
            time.sleep(1.0)

    def _branch_param(self) -> Dict[str, str]:
        if self.settings.branch_id:
            return {"branch_id": self.settings.branch_id}
        return {}


def _factories_from_templates(templates: Mapping[str, str]) -> Dict[str, TextFactory]:
    def build_factory(template: str) -> TextFactory:
        def factory(card_data: CardData) -> str:
            context = _build_template_context(card_data)
            return template.format_map(context)

        return factory

    return {name: build_factory(template) for name, template in templates.items()}


def _build_template_context(card_data: CardData) -> _TemplateContext:
    context: _TemplateContext = _TemplateContext(
        report_time=card_data.report.time,
        generated_at=card_data.generated_at,
        weather=card_data.weather,
        weather_temp_c=card_data.weather.temperature_c,
        weather_condition_code=card_data.weather.condition_code,
        weather_description=card_data.weather.condition_description,
        kerch_text=card_data.report.kerch_text or "",
        taman_text=card_data.report.taman_text or "",
        traffic_to_crimea=card_data.traffic_to_crimea,
        traffic_to_kuban=card_data.traffic_to_kuban,
    )
    return context


def _load_template_overrides(path: str | Path) -> Mapping[str, str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Файл с шаблонами слоёв не найден: {file_path}")
    content = file_path.read_text(encoding="utf-8")
    data = json.loads(content)
    if not isinstance(data, Mapping):
        raise ValueError("Файл с шаблонами должен содержать объект JSON")
    overrides: Dict[str, str] = {}
    for name, template in data.items():
        if isinstance(name, str) and isinstance(template, str):
            overrides[name] = template
    return overrides


def _require_env(name: str) -> str:
    from os import environ

    value = environ.get(name)
    if not value:
        raise RuntimeError(f"Требуется переменная окружения {name}")
    return value


def _optional_env(name: str) -> str | None:
    from os import environ

    value = environ.get(name)
    return value or None


__all__ = ["FigmaCardRenderer", "FigmaRenderSettings", "FigmaLayerConfig"]

