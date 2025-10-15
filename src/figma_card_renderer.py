"""Utilities for assembling bridge cards via the Figma REST API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

import json
import logging
import re
import time

import requests

from .models import CardData
from .ollama_helper import suggest_layer_mapping


LOGGER = logging.getLogger(__name__)


FIGMA_API_URL = "https://api.figma.com/v1"


TextFactory = Callable[[CardData], str]


def _normalise_text(value: str) -> str:
    return " ".join(value.split()).casefold()


_STOPWORDS = {"и", "на", "в", "со", "с", "по", "от", "до", "из", "для"}


def _tokenise(value: str) -> set[str]:
    tokens = set(re.findall(r"[\wё]+", value.casefold()))
    return {token for token in tokens if len(token) > 1 and token not in _STOPWORDS}


def _format_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")


def _format_time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _format_weekday(value: datetime) -> str:
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return weekdays[value.weekday()]


def _format_temperature(temp_c: float) -> str:
    return f"{temp_c:+.0f}°C"


def _format_score(score: int) -> str:
    suffix = "баллов" if score % 10 != 1 or score % 100 == 11 else "балл"
    return f"{score} {suffix}"


def _build_default_factories() -> Dict[str, TextFactory]:
    return {
        "День недели": lambda data: _format_weekday(data.report.time),
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


@dataclass(frozen=True)
class LayerConfiguration:
    templates: Dict[str, str] = field(default_factory=dict)
    aliases: Dict[str, list[str]] = field(default_factory=dict)
    weather_icon_layers: Dict[str, list[str]] = field(default_factory=dict)
    traffic_light_layers: Dict[str, Dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class _NodeInfo:
    node_id: str
    name: str
    type: str | None
    full_path: tuple[str, ...]
    normalized_name: str
    normalized_path: str

    def path_string(self) -> str:
        return "/".join(self.full_path)


@dataclass(frozen=True)
class _AliasInfo:
    aliases: set[str]
    tokens: set[str]
    preferred_type: str | None = "TEXT"

    def matches(self, node: _NodeInfo) -> bool:
        if self.preferred_type and node.type and node.type.upper() != self.preferred_type.upper():
            return False
        if node.normalized_name in self.aliases or node.normalized_path in self.aliases:
            return True
        for alias in self.aliases:
            if alias and alias in node.normalized_path:
                return True
        if self.tokens:
            if all(token in node.normalized_name for token in self.tokens):
                return True
            if all(token in node.normalized_path for token in self.tokens):
                return True
        return False


DEFAULT_LAYER_ALIASES: Dict[str, list[str]] = {
    "День недели": [
        "Дата и время/День",
        "Дата и время/День недели",
    ],
    "Дата": [
        "Дата и время/Дата",
        "Дата и время/Дата (текст)",
        "Дата и время/13.07",
    ],
    "Время": [
        "Дата и время/Время",
        "Дата и время/21:00",
        "Дата и время/Часы",
    ],
    "Обновлено": [
        "Дата и время/Обновлено",
        "Дата и время/Обновлено в",
    ],
    "Температура": [
        "Погода/Температура",
        "Погода/22",
        "Погода/°C",
        "Погода/Число",
    ],
    "Погодное описание": [
        "Погода/Описание",
        "Погода/Переменная облачность",
        "Погода/Текст",
        "Погода/Дождливо",
    ],
    "Баллы в Крым": [
        "Светофор Крым/Баллы",
        "Светофор Крым/Цифра",
        "Светофор Крым/Score",
        "в Крым",
    ],
    "Баллы на Кубань": [
        "Светофор Кубань/Баллы",
        "Светофор Кубань/Цифра",
        "Светофор Кубань/Score",
        "на Кубань",
    ],
    "Комментарий в Крым": [
        "Комментарий/в Крым",
        "Комментарий в Крым",
        "Со стороны Керчи",
        "Комментарий/Крым",
    ],
    "Комментарий на Кубань": [
        "Комментарий/на Кубань",
        "Комментарий на Кубань",
        "Со стороны Тамани",
        "Комментарий/Тамань",
    ],
}


DEFAULT_WEATHER_ICON_ALIASES: Dict[str, list[str]] = {
    "clear": ["Погода/Иконка/Ясно", "Погода/Ясно", "Погода/Солнце"],
    "partly": [
        "Погода/Иконка/Переменная облачность",
        "Погода/Переменная облачность",
        "Погода/Частично облачно",
    ],
    "cloudy": ["Погода/Иконка/Облачно", "Погода/Облачно", "Погода/Пасмурно"],
    "rain": ["Погода/Иконка/Дождь", "Погода/Дождь", "Погода/Дождливо"],
    "snow": ["Погода/Иконка/Снег", "Погода/Снег", "Погода/Снегопад"],
    "thunder": ["Погода/Иконка/Гроза", "Погода/Гроза"],
    "fog": ["Погода/Иконка/Туман", "Погода/Туман", "Погода/Мгла"],
}


DEFAULT_TRAFFIC_LIGHT_ALIASES: Dict[str, Dict[str, list[str]]] = {
    "в Крым": {
        "green": ["Светофор Крым/Зелёный", "Светофор Крым/Green"],
        "yellow": ["Светофор Крым/Жёлтый", "Светофор Крым/Yellow"],
        "red": ["Светофор Крым/Красный", "Светофор Крым/Red"],
    },
    "на Кубань": {
        "green": ["Светофор Кубань/Зелёный", "Светофор Кубань/Green"],
        "yellow": ["Светофор Кубань/Жёлтый", "Светофор Кубань/Yellow"],
        "red": ["Светофор Кубань/Красный", "Светофор Кубань/Red"],
    },
}


def _build_alias_info(
    name: str,
    extra_aliases: Iterable[str] | None = None,
    *,
    preferred_type: str | None = "TEXT",
) -> _AliasInfo:
    aliases = {_normalise_text(name)}
    tokens = _tokenise(name)
    for alias in extra_aliases or ():
        aliases.add(_normalise_text(alias))
        tokens.update(_tokenise(alias))
    return _AliasInfo(aliases=aliases, tokens=tokens, preferred_type=preferred_type)


def _weather_category(code: int) -> str:
    if code in {0, 1}:
        return "clear"
    if code == 2:
        return "partly"
    if code == 3:
        return "cloudy"
    if code in {45, 48}:
        return "fog"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 80, 81, 82}:
        return "rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "thunder"
    return "cloudy"


def _traffic_light_color(score: int) -> str:
    if score <= 3:
        return "green"
    if score <= 6:
        return "yellow"
    return "red"


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
        self._frame_nodes_cache: list[_NodeInfo] | None = None
        self._alias_overrides: Dict[str, list[str]] = {}
        self._weather_icon_aliases: Dict[str, list[str]] = {}
        self._traffic_light_aliases: Dict[str, Dict[str, list[str]]] = {}

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
        layer_config = LayerConfiguration()
        config_path = layer_config_path or _optional_env("FIGMA_LAYER_CONFIG")
        if config_path:
            layer_config = _load_layer_configuration(config_path)
            if layer_config.templates:
                factories.update(_factories_from_templates(layer_config.templates))

        settings = FigmaRenderSettings(
            token=token,
            file_key=file_key,
            frame_node_id=frame_node_id,
            branch_id=branch_id,
            frame_node_path=frame_node_path,
            frame_name_hint=frame_name_hint,
            text_factories=factories,
        )
        renderer = cls(settings)
        renderer._alias_overrides.update(layer_config.aliases)
        renderer._weather_icon_aliases.update(layer_config.weather_icon_layers)
        renderer._traffic_light_aliases.update(layer_config.traffic_light_layers)
        return renderer

    def render_card(self, card_data: CardData, *, poll_timeout: float = 30.0) -> bytes:
        text_values = {name: factory(card_data) for name, factory in self.settings.text_factories.items()}
        node_ids = self._resolve_node_ids(text_values.keys())
        updates = {node_id: text_values[name] for name, node_id in node_ids.items() if node_id}
        self._update_text_nodes(updates)
        self._apply_dynamic_layers(card_data)
        image_url = self._request_image_url()
        if not image_url:
            raise RuntimeError("Figma не вернула ссылку на изображение карточки")
        return self._download_image(image_url, poll_timeout=poll_timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    def _resolve_node_ids(self, layer_names: Iterable[str]) -> Dict[str, str | None]:
        self._get_frame_document(layer_names)
        nodes = self._ensure_frame_nodes()
        alias_map = self._build_alias_map(layer_names)
        lookup = {name: None for name in layer_names}
        for desired_name, alias_info in alias_map.items():
            for node in nodes:
                if lookup[desired_name] is None and alias_info.matches(node):
                    lookup[desired_name] = node.node_id
                    break
        missing = [name for name, node_id in lookup.items() if node_id is None]
        if missing:
            ai_mapping = self._suggest_nodes_with_ai(nodes, missing)
            if ai_mapping:
                for desired_name, suggested_layer in ai_mapping.items():
                    node_id = self._find_node_id_by_aliases([suggested_layer], nodes)
                    if node_id:
                        lookup[desired_name] = node_id
        still_missing = [name for name, node_id in lookup.items() if node_id is None]
        if still_missing:
            raise RuntimeError(
                "Не удалось найти слои в макете Figma: " + ", ".join(sorted(still_missing))
            )
        return lookup

    def _ensure_frame_nodes(self) -> list[_NodeInfo]:
        if self._frame_nodes_cache is None:
            _, frame_document = self._get_frame_document()
            self._frame_nodes_cache = list(self._collect_nodes(frame_document))
        return self._frame_nodes_cache

    def _collect_nodes(
        self, node: Mapping[str, object], path: tuple[str, ...] = tuple()
    ) -> Iterable[_NodeInfo]:
        if not isinstance(node, Mapping):
            return
        name = node.get("name")
        node_id = node.get("id")
        node_type = node.get("type")
        next_path = path
        if isinstance(name, str):
            next_path = path + (name,)
        if isinstance(node_id, str) and isinstance(name, str):
            type_value = node_type if isinstance(node_type, str) else None
            normalized_name = _normalise_text(name)
            normalized_path = _normalise_text("/".join(next_path)) if next_path else normalized_name
            yield _NodeInfo(
                node_id=node_id,
                name=name,
                type=type_value,
                full_path=next_path,
                normalized_name=normalized_name,
                normalized_path=normalized_path,
            )
        children = node.get("children")
        if isinstance(children, Sequence):
            for child in children:
                if isinstance(child, Mapping):
                    yield from self._collect_nodes(child, next_path)

    def _build_alias_map(self, layer_names: Iterable[str]) -> Dict[str, _AliasInfo]:
        alias_map: Dict[str, _AliasInfo] = {}
        for name in layer_names:
            extras = list(DEFAULT_LAYER_ALIASES.get(name, ()))
            extras.extend(self._alias_overrides.get(name, ()))
            alias_map[name] = _build_alias_info(name, extras)
        return alias_map

    def _find_node_id_by_aliases(
        self,
        alias_candidates: Iterable[str],
        nodes: Sequence[_NodeInfo],
        *,
        preferred_type: str | None = None,
    ) -> str | None:
        normalized_aliases = {
            _normalise_text(alias) for alias in alias_candidates if isinstance(alias, str) and alias
        }
        if not normalized_aliases:
            return None
        for node in nodes:
            if preferred_type and node.type and node.type.upper() != preferred_type.upper():
                continue
            if node.normalized_name in normalized_aliases or node.normalized_path in normalized_aliases:
                return node.node_id
            for alias in normalized_aliases:
                if alias and alias in node.normalized_path:
                    return node.node_id
        return None

    def _merged_weather_layers(self) -> Dict[str, list[str]]:
        mapping = {key: list(values) for key, values in DEFAULT_WEATHER_ICON_ALIASES.items()}
        for category, aliases in self._weather_icon_aliases.items():
            mapping.setdefault(category, []).extend(aliases)
        return mapping

    def _merged_traffic_layers(self) -> Dict[str, Dict[str, list[str]]]:
        mapping = {
            direction: {color: list(values) for color, values in layers.items()}
            for direction, layers in DEFAULT_TRAFFIC_LIGHT_ALIASES.items()
        }
        for direction, layers in self._traffic_light_aliases.items():
            target = mapping.setdefault(direction, {})
            for color, aliases in layers.items():
                target.setdefault(color, []).extend(aliases)
        return mapping

    def _apply_dynamic_layers(self, card_data: CardData) -> None:
        nodes = self._ensure_frame_nodes()
        visibility_updates: Dict[str, bool] = {}

        weather_layers = self._merged_weather_layers()
        if weather_layers:
            category = _weather_category(card_data.weather.condition_code)
            for layer_category, aliases in weather_layers.items():
                node_id = self._find_node_id_by_aliases(aliases, nodes, preferred_type=None)
                if node_id:
                    visibility_updates[node_id] = layer_category == category

        traffic_layers = self._merged_traffic_layers()
        if traffic_layers:
            for direction, layers in traffic_layers.items():
                score = self._resolve_direction_score(direction, card_data)
                if score is None:
                    continue
                target_color = _traffic_light_color(score)
                for color, aliases in layers.items():
                    node_id = self._find_node_id_by_aliases(aliases, nodes, preferred_type=None)
                    if node_id:
                        visibility_updates[node_id] = color == target_color

        if visibility_updates:
            self._update_visibility_nodes(visibility_updates)

    def _resolve_direction_score(self, direction: str, card_data: CardData) -> int | None:
        normalized_direction = _normalise_text(direction)
        direct_map = {
            _normalise_text(card_data.traffic_to_crimea.direction): card_data.traffic_to_crimea.score,
            _normalise_text(card_data.traffic_to_kuban.direction): card_data.traffic_to_kuban.score,
        }
        if normalized_direction in direct_map:
            return direct_map[normalized_direction]
        tokens = _tokenise(direction)
        if {"крым", "керч"}.intersection(tokens):
            return card_data.traffic_to_crimea.score
        if {"кубань", "кубан", "тамань", "таман"}.intersection(tokens):
            return card_data.traffic_to_kuban.score
        return None

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
                self._frame_nodes_cache = None
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
            self._frame_nodes_cache = None
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

    def _suggest_nodes_with_ai(
        self, frame_nodes: Sequence[_NodeInfo], missing: Sequence[str]
    ) -> Dict[str, str]:
        available_names = sorted({info.path_string() for info in frame_nodes if info.full_path})
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
        result: list[str] = []
        for info in self._collect_nodes(node):
            result.append(info.name)
            if info.full_path:
                result.append(info.path_string())
        return result

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

    def _update_visibility_nodes(self, visibility: Mapping[str, bool]) -> None:
        if not visibility:
            return
        payload = {
            "nodes": [
                {
                    "node_id": node_id,
                    "document": {"id": node_id, "visible": visible},
                }
                for node_id, visible in visibility.items()
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


def _load_layer_configuration(path: str | Path) -> LayerConfiguration:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Файл с настройками слоёв не найден: {file_path}")
    content = file_path.read_text(encoding="utf-8")
    data = json.loads(content)
    if not isinstance(data, Mapping):
        raise ValueError("Конфигурация слоёв должна быть объектом JSON")

    if data and all(isinstance(value, str) for value in data.values()):
        templates = {name: value for name, value in data.items() if isinstance(name, str)}
        return LayerConfiguration(templates=templates)

    def _as_str_list(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if isinstance(item, str)]
        return []

    templates: Dict[str, str] = {}
    raw_templates = data.get("templates")
    if isinstance(raw_templates, Mapping):
        for name, template in raw_templates.items():
            if isinstance(name, str) and isinstance(template, str):
                templates[name] = template

    aliases: Dict[str, list[str]] = {}
    raw_aliases = data.get("aliases")
    if isinstance(raw_aliases, Mapping):
        for name, alias_values in raw_aliases.items():
            if isinstance(name, str):
                alias_list = _as_str_list(alias_values)
                if alias_list:
                    aliases[name] = alias_list

    weather_layers: Dict[str, list[str]] = {}
    raw_weather = data.get("weather_icon_layers")
    if isinstance(raw_weather, Mapping):
        for category, layer_values in raw_weather.items():
            if isinstance(category, str):
                layer_list = _as_str_list(layer_values)
                if layer_list:
                    weather_layers[category] = layer_list

    traffic_layers: Dict[str, Dict[str, list[str]]] = {}
    raw_traffic = data.get("traffic_light_layers")
    if isinstance(raw_traffic, Mapping):
        for direction, color_map in raw_traffic.items():
            if not isinstance(direction, str) or not isinstance(color_map, Mapping):
                continue
            target: Dict[str, list[str]] = {}
            for color, layer_values in color_map.items():
                if isinstance(color, str):
                    layer_list = _as_str_list(layer_values)
                    if layer_list:
                        target[color] = layer_list
            if target:
                traffic_layers[direction] = target

    return LayerConfiguration(
        templates=templates,
        aliases=aliases,
        weather_icon_layers=weather_layers,
        traffic_light_layers=traffic_layers,
    )


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


__all__ = [
    "FigmaCardRenderer",
    "FigmaRenderSettings",
    "FigmaLayerConfig",
    "LayerConfiguration",
]

