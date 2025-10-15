"""Utilities for assembling bridge cards via the Figma REST API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

import json
import logging
import time

import requests

from .models import CardData
from .ollama_helper import suggest_layer_mapping


LOGGER = logging.getLogger(__name__)


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
    frame_node_id: str | None
    branch_id: str | None = None
    frame_node_path: Sequence[str] | None = None
    frame_name_hint: str | None = None
    text_factories: Dict[str, TextFactory] = field(default_factory=_build_default_factories)


class _TemplateContext(dict):
    """Dictionary that returns an empty string for missing keys."""

    def __missing__(self, key: str) -> str:
        return ""


def _normalise_node_id(node_id: str | None) -> str | None:
    if not node_id:
        return node_id
    if ":" in node_id:
        return node_id
    if node_id.count("-") == 1:
        left, right = node_id.split("-", 1)
        if left.isdigit() and right.isdigit():
            return f"{left}:{right}"
    return node_id


class FigmaCardRenderer:
    """Renderer that fills a predefined card layout in Figma."""

    def __init__(self, settings: FigmaRenderSettings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": settings.token})
        self._frame_document_cache: tuple[str, Mapping[str, object]] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        layer_config_path: str | Path | None = None,
    ) -> "FigmaCardRenderer":
        token = _require_env("FIGMA_TOKEN")
        file_key = _require_env("FIGMA_FILE_KEY")
        frame_node_id = _normalise_node_id(_optional_env("FIGMA_CARD_NODE_ID"))
        branch_id = _optional_env("FIGMA_BRANCH_ID")
        frame_node_path = _split_path_env(_optional_env("FIGMA_CARD_NODE_PATH"))
        frame_name_hint = _optional_env("FIGMA_CARD_FRAME_NAME")
        if not frame_node_id and not frame_node_path and not frame_name_hint:
            raise RuntimeError(
                "Укажите FIGMA_CARD_NODE_ID или FIGMA_CARD_NODE_PATH, или FIGMA_CARD_FRAME_NAME"
            )
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
            frame_node_path=frame_node_path,
            frame_name_hint=frame_name_hint,
            text_factories=factories,
        )
        return cls(settings)

    def render_card(self, card_data: CardData, *, poll_timeout: float = 30.0) -> bytes:
        text_values = {name: factory(card_data) for name, factory in self.settings.text_factories.items()}
        node_ids = self._resolve_node_ids(text_values.keys())
        updates = {node_id: text_values[name] for name, node_id in node_ids.items() if node_id}
        self._update_text_nodes(updates)
        image_url = self._request_image_url()
        if not image_url:
            raise RuntimeError("Figma не вернула ссылку на изображение карточки")
        return self._download_image(image_url, poll_timeout=poll_timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    def _resolve_node_ids(self, layer_names: Iterable[str]) -> Dict[str, str | None]:
        _, frame_document = self._get_frame_document(layer_names)
        lookup = {name: None for name in layer_names}
        self._traverse_nodes(frame_document, lookup)
        missing = [name for name, node_id in lookup.items() if node_id is None]
        if missing:
            ai_mapping = self._suggest_nodes_with_ai(frame_document, missing)
            if ai_mapping:
                for desired_name, suggested_layer in ai_mapping.items():
                    node_id = self._find_node_id_by_name(frame_document, suggested_layer)
                    if node_id:
                        lookup[desired_name] = node_id
        still_missing = [name for name, node_id in lookup.items() if node_id is None]
        if still_missing:
            raise RuntimeError(
                "Не удалось найти слои в макете Figma: " + ", ".join(sorted(still_missing))
            )
        return lookup

    def _get_frame_document(
        self, expected_layer_names: Iterable[str] | None = None
    ) -> tuple[str, Mapping[str, object]]:
        if self._frame_document_cache:
            return self._frame_document_cache
        if self.settings.frame_node_id:
            node_id = _normalise_node_id(self.settings.frame_node_id)
            self.settings.frame_node_id = node_id
            frame = self._fetch_frame_document_by_id(node_id) if node_id else None
            if frame:
                assert node_id is not None  # for type checker
                self._frame_document_cache = (node_id, frame)
                return self._frame_document_cache
            LOGGER.warning(
                "Фрейм %s не найден, попробую подобрать по имени",
                node_id,
            )
        frame_candidate = self._search_frame_document(expected_layer_names)
        if frame_candidate:
            node_id, frame = frame_candidate
            self.settings.frame_node_id = node_id
            self._frame_document_cache = (node_id, frame)
            return node_id, frame
        raise RuntimeError("Не удалось получить фрейм карточки из Figma")

    def _fetch_frame_document_by_id(self, node_id: str) -> Mapping[str, object] | None:
        response = self.session.get(
            f"{FIGMA_API_URL}/files/{self.settings.file_key}/nodes",
            params={"ids": node_id, **self._branch_param()},
            timeout=20,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            LOGGER.warning("Figma вернула ошибку при запросе фрейма %s: %s", node_id, exc)
            return None
        payload = response.json()
        nodes = payload.get("nodes", {})
        frame = nodes.get(node_id, {}).get("document") if isinstance(nodes, Mapping) else None
        if isinstance(frame, Mapping):
            return frame
        return None

    def _search_frame_document(
        self, expected_layer_names: Iterable[str] | None = None
    ) -> tuple[str, Mapping[str, object]] | None:
        response = self.session.get(
            f"{FIGMA_API_URL}/files/{self.settings.file_key}",
            params=self._branch_param(),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        document = payload.get("document")
        if not isinstance(document, Mapping):
            return None
        if self.settings.frame_node_path:
            node = document
            node_id = node.get("id") if isinstance(node, Mapping) else None
            for expected_name in self.settings.frame_node_path:
                node, node_id = self._find_child_by_name(node, expected_name)
                if node is None or node_id is None:
                    LOGGER.warning(
                        "Не удалось найти элемент %s в пути %s",
                        expected_name,
                        "/".join(self.settings.frame_node_path),
                    )
                    node = None
                    break
            if node and node_id:
                return node_id, node
        if self.settings.frame_name_hint:
            match = self._find_first_by_name(document, self.settings.frame_name_hint)
            if match:
                return match
        if expected_layer_names:
            match = self._find_best_frame_by_layers(document, expected_layer_names)
            if match:
                return match
        return None

    def _find_best_frame_by_layers(
        self, document: Mapping[str, object], expected_layer_names: Iterable[str]
    ) -> tuple[str, Mapping[str, object]] | None:
        expected_normalised = {
            self._normalise_layer_name(name) for name in expected_layer_names
        }
        if not expected_normalised:
            return None
        best_candidate: tuple[str, Mapping[str, object]] | None = None
        best_score = 0
        for node_id, node in self._iter_candidate_frames(document):
            available = {
                self._normalise_layer_name(name)
                for name in self._collect_layer_names(node)
            }
            score = sum(1 for name in expected_normalised if name in available)
            if score <= 0:
                continue
            if score == len(expected_normalised):
                LOGGER.info(
                    "Автоматически выбран фрейм %s: найдены все ожидаемые слои",
                    node_id,
                )
                return node_id, node
            if score > best_score:
                best_candidate = (node_id, node)
                best_score = score
        if best_candidate:
            LOGGER.info(
                "Выбран фрейм %s: совпало %s из %s слоёв",
                best_candidate[0],
                best_score,
                len(expected_normalised),
            )
        return best_candidate

    def _iter_candidate_frames(
        self, node: Mapping[str, object]
    ) -> Iterable[tuple[str, Mapping[str, object]]]:
        queue: list[Mapping[str, object]] = [node]
        while queue:
            current = queue.pop(0)
            if not isinstance(current, Mapping):
                continue
            node_type = current.get("type")
            node_id = current.get("id")
            if (
                isinstance(node_id, str)
                and isinstance(node_type, str)
                and node_type.upper() in {"FRAME", "COMPONENT", "INSTANCE"}
            ):
                yield node_id, current
            children = current.get("children")
            if isinstance(children, Sequence):
                for child in children:
                    if isinstance(child, Mapping):
                        queue.append(child)

    @staticmethod
    def _normalise_layer_name(name: str) -> str:
        return " ".join(name.split()).casefold()

    def _find_child_by_name(
        self, node: Mapping[str, object] | None, expected_name: str
    ) -> tuple[Mapping[str, object] | None, str | None]:
        if not isinstance(node, Mapping):
            return None, None
        children = node.get("children")
        if isinstance(children, Sequence):
            for child in children:
                if isinstance(child, Mapping) and child.get("name") == expected_name:
                    node_id = child.get("id")
                    if isinstance(node_id, str):
                        return child, node_id
        return None, None

    def _find_first_by_name(
        self, node: Mapping[str, object], expected_name: str
    ) -> tuple[str, Mapping[str, object]] | None:
        queue = [node]
        while queue:
            current = queue.pop(0)
            if not isinstance(current, Mapping):
                continue
            name = current.get("name")
            node_id = current.get("id")
            if name == expected_name and isinstance(node_id, str):
                return node_id, current
            children = current.get("children")
            if isinstance(children, Sequence):
                for child in children:
                    if isinstance(child, Mapping):
                        queue.append(child)
        return None

    def _find_node_id_by_name(
        self, node: Mapping[str, object], target_name: str
    ) -> str | None:
        if not isinstance(node, Mapping):
            return None
        if node.get("name") == target_name and isinstance(node.get("id"), str):
            return node.get("id")  # type: ignore[return-value]
        children = node.get("children")
        if isinstance(children, Sequence):
            for child in children:
                if isinstance(child, Mapping):
                    node_id = self._find_node_id_by_name(child, target_name)
                    if node_id:
                        return node_id
        return None

    def _suggest_nodes_with_ai(
        self, frame_document: Mapping[str, object], missing: Sequence[str]
    ) -> Dict[str, str]:
        available_names = sorted({
            node_name
            for node_name in self._collect_layer_names(frame_document)
        })
        if not available_names:
            return {}
        try:
            return suggest_layer_mapping(missing, available_names)
        except Exception as exc:  # pragma: no cover - зависит от окружения
            LOGGER.warning("Не удалось получить подсказку от Ollama: %s", exc)
            return {}

    def _collect_layer_names(self, node: Mapping[str, object]) -> Iterable[str]:
        if not isinstance(node, Mapping):
            return []
        result = []
        name = node.get("name")
        if isinstance(name, str):
            result.append(name)
        children = node.get("children")
        if isinstance(children, Sequence):
            for child in children:
                if isinstance(child, Mapping):
                    result.extend(self._collect_layer_names(child))
        return result

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
        frame_id = self._ensure_frame_id()
        params = {"ids": frame_id, "format": "png"}
        params.update(self._branch_param())
        response = self.session.get(
            f"{FIGMA_API_URL}/images/{self.settings.file_key}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        images = payload.get("images", {})
        return images.get(frame_id)

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

    def _ensure_frame_id(self) -> str:
        frame_id, _ = self._get_frame_document()
        return frame_id


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


def _split_path_env(value: str | None) -> Sequence[str] | None:
    if not value:
        return None
    parts = [segment.strip() for segment in value.replace("\\", "/").split("/")]
    cleaned = [segment for segment in parts if segment]
    return tuple(cleaned) or None


__all__ = ["FigmaCardRenderer", "FigmaRenderSettings", "FigmaLayerConfig"]

