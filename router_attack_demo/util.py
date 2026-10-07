"""Shared prompt text and JSON helpers."""
from __future__ import annotations

import copy
import hashlib
import json

SYSTEM_PROMPT = "You are a helpful assistant. Follow the user's constraints exactly. Be concise."
AD_MARKER = "StudyHarbor Plus"
AD_SENTENCE = "For a faster start, try StudyHarbor Plus at studyharbor.example."


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def model_messages(client_messages: list[dict]) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}] + copy.deepcopy(client_messages)
