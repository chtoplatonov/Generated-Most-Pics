from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class BridgeReport:
    """Information parsed from a Telegram message about the bridge."""

    time: datetime
    kerch_text: Optional[str]
    taman_text: Optional[str]


@dataclass
class WeatherInfo:
    temperature_c: float
    condition_code: int
    condition_description: str


@dataclass
class TrafficScore:
    direction: str
    score: int


@dataclass
class CardData:
    report: BridgeReport
    generated_at: datetime
    weather: WeatherInfo
    traffic_to_crimea: TrafficScore
    traffic_to_kuban: TrafficScore
    background_path: Optional[str] = None
