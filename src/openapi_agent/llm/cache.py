"""On-disk response cache keyed by endpoint-metadata hash.

Cache entries are plain JSON files; the key is a sha256 of provider, model,
prompt version, and the compact metadata payload — identical inputs never hit
the provider twice. API keys are never part of keys or stored content.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel

from openapi_agent.logging_utils import get_logger

log = get_logger("llm.cache")

M = TypeVar("M", bound=BaseModel)


class ResponseCache:
    def __init__(self, cache_dir: Path | None) -> None:
        self.dir = Path(cache_dir) if cache_dir else None
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("cache dir unusable (%s); caching disabled", exc)
                self.dir = None

    @staticmethod
    def key(provider: str, model: str, prompt_version: str, payload: str) -> str:
        material = "\x1f".join([provider, model, prompt_version, payload])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path | None:
        return self.dir / f"{key}.json" if self.dir is not None else None

    def get(self, key: str, model_cls: type[M]) -> Optional[M]:
        path = self._path(key)
        if path is None or not path.is_file():
            return None
        try:
            return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - stale/corrupt entries are ignored
            return None

    def put(self, key: str, value: BaseModel) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            path.write_text(value.model_dump_json(), encoding="utf-8")
        except OSError as exc:
            log.debug("cache write failed: %s", exc)

    def get_text(self, key: str) -> str | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        except Exception:  # noqa: BLE001
            return None

    def put_text(self, key: str, text: str) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            path.write_text(json.dumps({"text": text}), encoding="utf-8")
        except OSError as exc:
            log.debug("cache write failed: %s", exc)
