"""Tiny JSON-file store: loaded once into memory, written atomically."""
import asyncio
import json
import logging
import os
from typing import Any, Callable

log = logging.getLogger("assembly_bot.storage")


class JsonStore:
    def __init__(
        self,
        path: str,
        default: Callable[[], dict[str, Any]],
        normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self._default = default
        self._normalize = normalize
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return self._finish(self._default())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not read %s (%s); using defaults", self.path, e)
            return self._finish(self._default())

        if not isinstance(raw, dict):
            return self._finish(self._default())
        merged = self._default()
        merged.update(raw)
        return self._finish(merged)

    def _finish(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._normalize(data) if self._normalize else data

    def _write(self, payload: str) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomic: a crash can't leave a half-written file

    async def save(self) -> None:
        async with self._lock:
            payload = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
            try:
                await asyncio.to_thread(self._write, payload)
            except OSError:
                log.exception("Failed to save %s", self.path)
