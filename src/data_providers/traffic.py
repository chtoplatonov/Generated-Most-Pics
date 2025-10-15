from __future__ import annotations

from dataclasses import dataclass
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
    api_key: str
    directions: Iterable[DirectionConfig]
    timeout: int = 10


class YandexTrafficClient:
    """Fetch traffic information (jam scores) from the Yandex Maps API."""

    BASE_URL = "https://api-maps.yandex.ru/services/traffic-info/2.1/"

    def __init__(self, config: TrafficConfig):
        self.config = config

    def fetch_scores(self) -> list[TrafficScore]:
        scores: list[TrafficScore] = []
        for direction in self.config.directions:
            score = self._fetch_direction_score(direction)
            scores.append(TrafficScore(direction=direction.name, score=score))
        return scores

    def _fetch_direction_score(self, direction: DirectionConfig) -> int:
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
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                raise ValueError(
                    "Yandex Traffic API вернул 404. Проверьте, что ключ активирован и поддерживает сервис пробок."
                ) from exc
            raise
        payload = response.json()
        score = self._extract_score(payload)
        return _clamp_score(score)

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


def _clamp_score(value: int) -> int:
    return max(0, min(10, int(round(value))))
