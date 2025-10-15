from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

import requests

from ..models import TrafficScore


@dataclass(frozen=True)
class DirectionConfig:
    name: str
    longitude: float
    latitude: float
    radius: int = 3000


@dataclass(frozen=True)
class TrafficConfig:
    directions: Iterable[DirectionConfig]
    api_key: Optional[str] = None
    timeout: int = 10


LOGGER = logging.getLogger(__name__)


class YandexTrafficClient:
    """Fetch traffic information (jam scores) from public Yandex sources."""

    BASE_URL = "https://api-maps.yandex.ru/services/traffic-info/2.1/"
    REGINFO_URL = "https://export.yandex.ru/bar/reginfo.xml"
    FALLBACK_URL = "https://yandex.ru/maps/traffic/"

    def __init__(self, config: TrafficConfig):
        self.config = config

    def fetch_scores(self) -> list[TrafficScore]:
        scores: list[TrafficScore] = []
        for direction in self.config.directions:
            score = self._fetch_direction_score(direction)
            scores.append(TrafficScore(direction=direction.name, score=score))
        return scores

    def _fetch_direction_score(self, direction: DirectionConfig) -> int:
        try:
            return self._fetch_direction_score_via_reginfo(direction)
        except Exception as reg_exc:  # pragma: no cover - network dependent
            LOGGER.warning(
                (
                    "Не удалось получить данные пробок через открытый сервис Яндекса для направления %s: %s. "
                    "Пробую другие источники."
                ),
                direction.name,
                reg_exc,
            )

        if self.config.api_key:
            try:
                return self._fetch_direction_score_via_api(direction)
            except Exception as exc:  # pragma: no cover - fallback path depends on network
                LOGGER.warning(
                    (
                        "Не удалось получить пробки через API Яндекса для направления %s: %s. "
                        "Перехожу к парсингу сайта."
                    ),
                    direction.name,
                    exc,
                )

        try:
            return self._fetch_direction_score_via_scraper(direction)
        except Exception as fallback_exc:  # pragma: no cover - network dependent
            LOGGER.warning(
                (
                    "Не удалось получить данные пробок с сайта Яндекса для направления %s: %s. "
                    "Возвращаю значение 0."
                ),
                direction.name,
                fallback_exc,
            )
            LOGGER.debug("Ошибка парсинга сайта Яндекса", exc_info=fallback_exc)
            return 0

    def _fetch_direction_score_via_reginfo(self, direction: DirectionConfig) -> int:
        params = {"lat": direction.latitude, "lon": direction.longitude}
        response = requests.get(self.REGINFO_URL, params=params, timeout=self.config.timeout)
        response.raise_for_status()

        text = response.text

        try:
            tree = ET.fromstring(response.content)
        except ET.ParseError:
            tree = None

        if tree is not None:
            level = self._extract_score_from_tree(tree)
            if level is not None:
                score = _clamp_score(level)
                LOGGER.info(
                    "Получил данные пробок через открытый сервис Яндекса для направления %s: %s баллов",
                    direction.name,
                    score,
                )
                return score

        score = _clamp_score(self._extract_score_from_reginfo_text(text))
        LOGGER.info(
            "Получил данные пробок через открытый сервис Яндекса для направления %s: %s баллов",
            direction.name,
            score,
        )
        return score

    def _fetch_direction_score_via_api(self, direction: DirectionConfig) -> int:
        params = {
            "format": "json",
            "lang": "ru_RU",
            "origin": "maps-api",
            "type": "probki",
            "ll": f"{direction.longitude},{direction.latitude}",
            "radius": direction.radius,
            "apikey": self.config.api_key,
        }
        response = requests.get(self.BASE_URL, params=params, timeout=self.config.timeout)
        response.raise_for_status()
        payload = response.json()
        score = self._extract_score(payload)
        return _clamp_score(score)

    def _fetch_direction_score_via_scraper(self, direction: DirectionConfig) -> int:
        params = {
            "ll": f"{direction.longitude},{direction.latitude}",
            "z": 12,
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        }
        response = requests.get(
            self.FALLBACK_URL,
            params=params,
            headers=headers,
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        score = self._extract_score_from_html(response.text)
        LOGGER.info(
            "Получил данные пробок с сайта Яндекса для направления %s: %s баллов",
            direction.name,
            score,
        )
        return _clamp_score(score)

    @staticmethod
    def _extract_score_from_tree(tree: ET.Element) -> Optional[int]:
        candidates: list[int] = []
        tags_of_interest = {"level", "rate", "value", "points", "ball", "score"}

        for element in tree.iter():
            if element.tag.lower() in tags_of_interest:
                text = (element.text or "").strip()
                if text.isdigit():
                    candidates.append(int(text))

        if candidates:
            return candidates[0]
        return None

    @staticmethod
    def _extract_score_from_reginfo_text(text: str) -> int:
        patterns = [
            r"<level[^>]*>([0-9]+)</level>",
            r"<rate[^>]*>([0-9]+)</rate>",
            r"<value[^>]*>([0-9]+)</value>",
            r"<points[^>]*>([0-9]+)</points>",
            r"<ball[^>]*>([0-9]+)</ball>",
            r"([0-9]+)\s*балл",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        raise ValueError("Не удалось определить баллы пробок в открытом ответе Яндекса")

    @staticmethod
    def _extract_score(payload: dict) -> int:
        """Attempt to extract the most relevant traffic score from the response."""

        # Preferred locations in the response. The structure differs between API
        # versions, so we try a range of likely paths and fall back to a generic
        # search for a value that resembles a jam score.
        candidates: list[Optional[int]] = []

        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            region = data.get("region")
            if isinstance(region, dict):
                rating = region.get("rating")
                if isinstance(rating, (int, float)):
                    candidates.append(int(round(rating)))
            traffic = data.get("traffic")
            if isinstance(traffic, dict):
                level = traffic.get("level")
                if isinstance(level, (int, float)):
                    candidates.append(int(round(level)))
                popular = traffic.get("popular")
                if isinstance(popular, dict):
                    value = popular.get("level")
                    if isinstance(value, (int, float)):
                        candidates.append(int(round(value)))

        if candidates:
            filtered = [value for value in candidates if value is not None]
            if filtered:
                return filtered[0]

        # Generic fallback: walk the payload and look for keys that suggest a
        # traffic level. This ensures we can still work with minor schema
        # changes.
        stack = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key.lower() in {"rating", "level", "score", "ball", "jamlevel"} and isinstance(value, (int, float)):
                        return int(round(value))
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)

        raise ValueError("Unable to determine traffic score from response")

    @staticmethod
    def _extract_score_from_html(html: str) -> int:
        patterns = [
            r"([0-9]+)\s*балл",
            r'"traffic(?:Level|Score|Rating)"\s*:\s*([0-9]+)',
            r'"jamLevel"\s*:\s*([0-9]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        raise ValueError("Не удалось определить баллы пробок на странице Яндекса")


def _clamp_score(value: int) -> int:
    return max(0, min(10, int(round(value))))
