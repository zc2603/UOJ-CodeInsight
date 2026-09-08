from __future__ import annotations

import json
import re

from app.integrations.uoj.schemas import ParsedSubmissionContent


_STORAGE_PATH = re.compile(r"^/submission/([0-9]+)/([A-Za-z0-9]+)$")


class UOJSubmissionContentError(ValueError):
    pass


class UOJSubmissionContentParser:
    def parse(self, raw: str) -> ParsedSubmissionContent:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UOJSubmissionContentError("content is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise UOJSubmissionContentError("content JSON must be an object")

        storage_path = payload.get("file_name")
        if not isinstance(storage_path, str):
            raise UOJSubmissionContentError("file_name is missing")
        match = _STORAGE_PATH.fullmatch(storage_path)
        if not match:
            raise UOJSubmissionContentError("unsafe or unsupported submission path")

        raw_config = payload.get("config", [])
        if not isinstance(raw_config, list):
            raise UOJSubmissionContentError("config must be a list")
        config: dict[str, str] = {}
        for pair in raw_config:
            if not isinstance(pair, list) or len(pair) != 2:
                raise UOJSubmissionContentError("invalid config entry")
            key, value = pair
            if not isinstance(key, str) or not isinstance(value, str):
                raise UOJSubmissionContentError("config keys and values must be strings")
            config[key] = value

        return ParsedSubmissionContent(
            storage_path=storage_path,
            bucket=match.group(1),
            random_id=match.group(2),
            config=config,
        )
