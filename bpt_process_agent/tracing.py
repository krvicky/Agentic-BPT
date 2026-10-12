"""Structured trace serialization and conservative secret redaction."""
from dataclasses import fields, is_dataclass
import hashlib
import json
import math
import re

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"^(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|client[_-]?secret|authorization|credentials?|private[_-]?key)$", re.I)
_SECRET_TEXT = re.compile(r"\bsk-[A-Za-z0-9_-]+|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*|\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*[^\s,;]+", re.I)

def redact(value: object, depth: int = 0) -> object:
    if depth > 30:
        return "[DEPTH_LIMIT]"
    if is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in fields(value)}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key) if isinstance(key, (str, int, float, bool)) else "[INVALID_KEY]"
            name = _SECRET_TEXT.sub(REDACTED, name)
            result[name] = REDACTED if _SENSITIVE_KEY.fullmatch(name) else redact(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(REDACTED, value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else "[NONFINITE]"
    return "[UNSUPPORTED_VALUE]"

def contains_secret(value: object, depth: int = 0) -> bool:
    if depth > 30:
        return True
    if is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in fields(value)}
    if isinstance(value, dict):
        return any(
            isinstance(key, str) and (_SENSITIVE_KEY.fullmatch(key) and item != REDACTED or _SECRET_TEXT.search(key))
            or contains_secret(item, depth + 1) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item, depth + 1) for item in value)
    return isinstance(value, str) and _SECRET_TEXT.search(value) is not None

def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def safe_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(redact(value)).encode()).hexdigest()
