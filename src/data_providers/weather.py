from __future__ import annotations

from dataclasses import dataclass
import logging
import xml.etree.ElementTree as ET
from typing import Dict

import requests

from ..models import WeatherInfo


@dataclass(frozen=True)
class WeatherConfig:
    latitude: float = 45.3019
    longitude: float = 36.5134
    timezone: str = "Europe/Moscow"


WEATHER_CODE_DESCRIPTIONS: Dict[int, str] = {
    0: "Ясно",
    1: "Преимущественно ясно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Туман",
    51: "Лёгкая морось",
    53: "Морось",
    55: "Сильная морось",
    56: "Лёгкий ледяной дождь",
    57: "Ледяной дождь",
    61: "Лёгкий дождь",
    63: "Дождь",
    65: "Сильный дождь",
    66: "Лёгкий ледяной дождь",
    67: "Сильный ледяной дождь",
    71: "Лёгкий снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снег",
    80: "Кратковременные дожди",
    81: "Сильные ливни",
    82: "Очень сильные ливни",
    85: "Снегопад",
    86: "Сильный снегопад",
    95: "Гроза",
    96: "Гроза",
    99: "Гроза",
}


LOGGER = logging.getLogger(__name__)


class WeatherClient:
    """Fetch current weather using the Open-Meteo API."""

    def __init__(self, config: WeatherConfig | None = None):
        self.config = config or WeatherConfig()

    def fetch(self) -> WeatherInfo:
        try:
            return self._fetch_from_open_meteo()
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning(
                "Не удалось получить погоду из Open-Meteo: %s. Перехожу к данным с сайта Яндекса.",
                exc,
            )
            return self._fetch_from_yandex()

    def _fetch_from_open_meteo(self) -> WeatherInfo:
        cfg = self.config
        params = {
            "latitude": cfg.latitude,
            "longitude": cfg.longitude,
            "current": ["temperature_2m", "weather_code"],
            "timezone": cfg.timezone,
        }
        response = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        current = payload.get("current") or payload.get("current_weather") or {}
        if "temperature_2m" not in current or "weather_code" not in current:
            raise ValueError("Weather response did not contain temperature or weather code")

        temp = float(current.get("temperature_2m"))
        code = int(current.get("weather_code"))
        description = WEATHER_CODE_DESCRIPTIONS.get(code, "Погода")
        return WeatherInfo(temperature_c=temp, condition_code=code, condition_description=description)

    def _fetch_from_yandex(self) -> WeatherInfo:
        cfg = self.config
        params = {"lat": cfg.latitude, "lon": cfg.longitude}
        response = requests.get("https://export.yandex.ru/bar/reginfo.xml", params=params, timeout=10)
        response.raise_for_status()
        try:
            tree = ET.fromstring(response.content)
        except ET.ParseError as exc:  # pragma: no cover - depends on remote format
            raise ValueError("Некорректный ответ Яндекса с данными погоды") from exc

        fact = tree.find(".//fact")
        if fact is None:
            raise ValueError("Не удалось найти блок fact в ответе Яндекса")

        temp_text = fact.findtext("temperature") or fact.findtext("temperature_air")
        condition_text = fact.findtext("weather_type_short") or fact.findtext("weather_type")
        icon_text = fact.findtext("weather_icon")

        if temp_text is None:
            raise ValueError("Ответ Яндекса не содержит температуру")

        try:
            temperature = float(temp_text.replace(",", "."))
        except ValueError as exc:  # pragma: no cover - depends on remote format
            raise ValueError(f"Не удалось преобразовать температуру '{temp_text}'") from exc

        code = _yandex_condition_to_code(condition_text, icon_text)
        description = condition_text or "Погода"
        return WeatherInfo(temperature_c=temperature, condition_code=code, condition_description=description)


def _yandex_condition_to_code(condition: str | None, icon: str | None) -> int:
    if icon:
        icon_key = icon.strip().lower()
        icon_map = {
            "skc_d": 0,
            "skc_n": 0,
            "bkn_d": 2,
            "bkn_n": 2,
            "ovc": 3,
            "ovc_-ra": 63,
            "ovc_+ra": 65,
            "ovc_ra": 63,
            "ovc_tsra": 95,
            "ovc_-sn": 71,
            "ovc_sn": 73,
            "ovc_+sn": 75,
            "ovc_-tsra": 95,
            "ovc_ts": 95,
            "ovc_ra_sn": 85,
            "ovc_-ra_sn": 71,
            "ovc_+ra_sn": 75,
            "ovc_fog": 45,
            "ovc_-dz": 51,
            "ovc_dz": 53,
            "ovc_+dz": 55,
        }
        if icon_key in icon_map:
            return icon_map[icon_key]

    normalized = (condition or "").strip().lower()
    text_map = {
        "ясно": 0,
        "солнечно": 0,
        "малооблачно": 2,
        "переменная облачность": 2,
        "облачно с прояснениями": 2,
        "облачно": 3,
        "пасмурно": 3,
        "туман": 45,
        "морось": 53,
        "небольшой дождь": 61,
        "дождь": 63,
        "сильный дождь": 65,
        "ливень": 82,
        "небольшой снег": 71,
        "снег": 73,
        "сильный снег": 75,
        "снегопад": 85,
        "гроза": 95,
    }
    return text_map.get(normalized, 3)
