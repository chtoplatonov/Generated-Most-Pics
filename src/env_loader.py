"""Utility helpers for loading environment variables from a .env file."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable


LOGGER = logging.getLogger(__name__)


def load_dotenv(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
    logger: logging.Logger | None = None,
) -> None:
    """Populate ``os.environ`` values from a ``.env`` file.

    Parameters
    ----------
    path:
        Custom path to the ``.env`` file. If ``None``, ``.env`` in the
        project root is used.
    override:
        When ``True`` the values from the file replace existing variables.
        By default the function keeps already defined variables intact.
    logger:
        Optional logger used for diagnostic messages. When omitted a module
        level logger is used.
    """

    env_path = Path(path or ".env")
    active_logger = logger or LOGGER

    if not env_path.exists():
        return

    try:
        for key, value in _iter_env_lines(env_path):
            if override or key not in os.environ:
                os.environ[key] = value
        active_logger.info("Значения окружения загружены из %s", env_path)
    except OSError as exc:  # pragma: no cover - зависит от окружения
        active_logger.warning("Не удалось прочитать %s: %s", env_path, exc)


def _iter_env_lines(path: Path) -> Iterable[tuple[str, str]]:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if (
            cleaned_value
            and len(cleaned_value) >= 2
            and cleaned_value[0] == cleaned_value[-1]
            and cleaned_value[0] in {'"', "'"}
        ):
            cleaned_value = cleaned_value[1:-1]
        if cleaned_key:
            yield cleaned_key, cleaned_value


__all__ = ["load_dotenv"]

