from __future__ import annotations

import re
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


def _split_fragments(text: str) -> list[str]:
    """Split a block of text into meaningful fragments.

    The channel messages are usually separated by newlines, but when a user
    copies the text manually the direction sentences can end up on the same
    line.  We therefore split by newlines first and then by sentence
    boundaries, trimming bullet characters like "-" or "•".
    """

    fragments: list[str] = []
    raw_blocks = re.split(r"[\r\n]+", text)
    for block in raw_blocks:
        cleaned = block.strip()
        if not cleaned:
            continue
        # Further split combined sentences while keeping punctuation.
        sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z])", cleaned)
        for sentence in sentences:
            normalized = sentence.strip(" \t-–—•")
            if normalized:
                fragments.append(normalized)
    return fragments


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

    lines = [line for line in message.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Message contains no meaningful content")

    first_line = lines[0].strip()
    time_match = re.search(r"(\d{1,2}[:.]\d{2})", first_line)
    if not time_match:
        raise ValueError(f"Could not detect report time in message: {first_line!r}")

    report_time = _parse_time(time_match.group(1))
    tzinfo = tz.gettz(timezone)
    if tzinfo is None:
        raise ValueError(f"Unknown timezone: {timezone}")

    now = datetime.now(tzinfo)
    report_datetime = datetime.combine(now.date(), report_time, tzinfo=tzinfo)

    # Collect the remaining content from the first line (after the time) and
    # every other line.
    suffix = first_line[time_match.end() :].strip(" \t-–—•:;,")
    remaining_lines = []
    if suffix:
        remaining_lines.append(suffix)
    for extra in lines[1:]:
        remaining_lines.append(extra.strip())

    fragments = _split_fragments("\n".join(remaining_lines))

    kerch_text, taman_text = _extract_direction_texts(fragments)

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
