"""Helpers for interacting with a local Ollama instance."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Iterable

import requests


LOGGER = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def suggest_layer_mapping(
    requested_layers: Iterable[str],
    available_layers: Iterable[str],
    *,
    model: str | None = None,
    endpoint: str | None = None,
    timeout: int = 60,
) -> dict[str, str]:
    """Ask a local Ollama model to match desired names to available layers.

    The function expects an Ollama instance to be running locally (by default on
    ``http://127.0.0.1:11434``). When the model or endpoint variables are not
    provided, the helper tries to read them from the environment variables
    ``OLLAMA_MODEL`` and ``OLLAMA_ENDPOINT``. If neither is defined, an empty
    mapping is returned.
    """

    requested = list(dict.fromkeys(requested_layers))
    available = list(dict.fromkeys(available_layers))
    if not requested or not available:
        return {}

    model_name = model or os.environ.get("OLLAMA_MODEL")
    if not model_name:
        return {}
    endpoint_url = (endpoint or os.environ.get("OLLAMA_ENDPOINT") or "http://127.0.0.1:11434").rstrip("/")

    prompt = _build_prompt(requested, available)
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }

    response = requests.post(
        f"{endpoint_url}/api/generate",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    text = data.get("response", "")
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _load_json_fallback(text)
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            if key in requested and value in available:
                result[key] = value
    return result


def _build_prompt(requested: list[str], available: list[str]) -> str:
    return (
        "Ты помогаешь связать поля карточки с слоями в макете Figma.\n"
        "Ниже два списка. Первый — какие текстовые поля надо заполнить.\n"
        "Второй — какие названия слоёв доступны в макете.\n"
        "Верни JSON-объект, где ключ — название поля, а значение — лучше всего подходящий слой.\n"
        "Если подходящего слоя нет, просто не добавляй поле в ответ.\n"
        f"Поля: {json.dumps(requested, ensure_ascii=False)}\n"
        f"Слои: {json.dumps(available, ensure_ascii=False)}\n"
        "Ответ:"
    )


def _load_json_fallback(text: str) -> dict[str, str] | None:
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


__all__ = ["suggest_layer_mapping"]
