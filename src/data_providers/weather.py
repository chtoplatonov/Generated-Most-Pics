from __future__ import annotations

from dataclasses import dataclass
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


class WeatherClient:
    """Fetch current weather using the Open-Meteo API."""

    def __init__(self, config: WeatherConfig | None = None):
        self.config = config or WeatherConfig()

    def fetch(self) -> WeatherInfo:
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
