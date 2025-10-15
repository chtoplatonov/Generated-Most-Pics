from __future__ import annotations

from datetime import datetime, time
from typing import Iterable, Tuple

from dateutil import tz

from .models import BridgeReport


TIME_FORMATS = ["%H:%M", "%H.%M"]


def _parse_time(value: str) -> time:
    value = value.strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unsupported time format: {value!r}")


def parse_bridge_message(message: str, *, timezone: str = "Europe/Moscow") -> BridgeReport:
    """Parse a Telegram message about the Crimean bridge.

    Parameters
    ----------
    message:
        Raw text message from the Telegram channel.
    timezone:
        Name of the timezone in which the reported time should be interpreted.
    """

    if not message.strip():
        raise ValueError("Message is empty")

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Message contains no meaningful content")

    report_time = _parse_time(lines[0])
    tzinfo = tz.gettz(timezone)
    if tzinfo is None:
        raise ValueError(f"Unknown timezone: {timezone}")

    now = datetime.now(tzinfo)
    report_datetime = datetime.combine(now.date(), report_time, tzinfo=tzinfo)

    kerch_text, taman_text = _extract_direction_texts(lines[1:])

    return BridgeReport(time=report_datetime, kerch_text=kerch_text, taman_text=taman_text)


def _extract_direction_texts(lines: Iterable[str]) -> Tuple[str | None, str | None]:
    kerch_text = None
    taman_text = None
    for line in lines:
        normalized = line.lower()
        if "со стороны керч" in normalized or "в крым" in normalized:
            kerch_text = line
        elif "со стороны таман" in normalized or "на кубан" in normalized:
            taman_text = line
    return kerch_text, taman_text
