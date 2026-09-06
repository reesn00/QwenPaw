# -*- coding: utf-8 -*-
"""Data models for trajectory events."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TrajectoryEventType(str, Enum):
    """Kinds of trajectory events persisted for one turn."""

    TURN_START = "turn_start"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL_REQUEST = "tool_call_request"
    TOOL_EXECUTION = "tool_execution"
    THINKING = "thinking"
    ERROR = "error"
    CANCEL = "cancel"
    FINAL_REPLY = "final_reply"


class TrajectoryConfig(BaseModel):
    """Per-agent trajectory recording configuration."""

    enabled: bool = Field(default=True)
    max_record_bytes: int = Field(default=256 * 1024, ge=0)
    retention_days: int = Field(default=30, ge=0)
    flush_interval_seconds: int = Field(default=10, ge=1)
    redact_patterns: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class TrajectoryEvent(BaseModel):
    """One row in the trajectory log."""

    trace_id: str = Field(..., description="Request-level trace identifier.")
    span_id: str = Field(default_factory=lambda: uuid4().hex)
    parent_span_id: Optional[str] = Field(
        default=None,
        description="Parent event span, e.g. request -> response.",
    )
    event_type: TrajectoryEventType
    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(),
    )

    session_id: str = ""
    agent_id: str = ""
    user_id: str = ""
    channel: str = ""

    provider_id: str = ""
    model_name: str = ""

    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")


# Built-in redaction patterns for API keys, tokens, secrets, passwords.
# Each pattern captures the key/prefix in group 1 and the sensitive value in
# group 2 so the replacement can keep the prefix and only redact the secret.
_DEFAULT_REDACT_PATTERNS = [
    re.compile(
        r"(api[_\-]?key\s*[:=]\s*[\"']?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(x-api-key\s*[:=]\s*[\"']?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(authorization\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(token\s*[:=]\s*[\"']?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(secret\s*[:=]\s*[\"']?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(password\s*[:=]\s*[\"']?)([^\s,;\"'\]\}]+)",
        re.IGNORECASE,
    ),
]


def _maybe_truncate_text(text: Any, max_bytes: int) -> Any:
    """Truncate a string to *max_bytes* UTF-8 length, adding a marker."""
    if not isinstance(text, str):
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    keep = max_bytes - len(" ...[truncated]".encode("utf-8"))
    if keep <= 0:
        return "...[truncated]"
    # Truncate on a byte boundary safely.
    truncated = encoded[:keep].decode("utf-8", errors="ignore")
    return truncated + " ...[truncated]"


def _redact_value(value: Any, patterns: list[re.Pattern[str]]) -> Any:
    """Recursively redact sensitive strings in *value*."""
    if isinstance(value, str):
        result = value
        for pattern in patterns:
            result = pattern.sub(r"\1[redacted]", result)
        return result
    if isinstance(value, dict):
        # Also redact values whose keys are sensitive.
        redacted: dict[str, Any] = {}
        for k, v in value.items():
            lowered = str(k).lower()
            if any(
                token in lowered
                for token in (
                    "api_key",
                    "api-key",
                    "authorization",
                    "token",
                    "secret",
                    "password",
                )
            ) and isinstance(v, str):
                redacted[k] = "[redacted]"
            else:
                redacted[k] = _redact_value(v, patterns)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, patterns) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, patterns) for item in value)
    return value


def _maybe_redact_media(value: Any, max_bytes: int) -> Any:
    """Replace large base64 media blobs with a length summary."""
    if isinstance(value, list):
        return [_maybe_redact_media(item, max_bytes) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for k, v in value.items():
        if k in ("data", "image_url", "audio_url", "video_url") and isinstance(v, str):
            encoded = v.encode("utf-8")
            if len(encoded) > max_bytes:
                result[k] = f"<{len(encoded)} bytes media omitted>"
                continue
        result[k] = _maybe_redact_media(v, max_bytes)
    return result


def _make_json_safe(value: Any) -> Any:
    """Recursively coerce common non-JSON types to plain JSON values."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        try:
            return value.model_dump(mode="json")
        except Exception:
            try:
                return dict(value)
            except Exception:
                return str(value)
    if isinstance(value, dict):
        return {str(k): _make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_make_json_safe(item) for item in value]
    # For objects with a model_dump method (e.g. ChatUsage).
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except Exception:
            pass
    try:
        return dict(value)
    except Exception:
        return str(value)


def sanitize_payload(
    payload: dict[str, Any],
    max_record_bytes: int,
    extra_patterns: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Return a privacy-safe, size-capped copy of *payload*."""
    patterns: list[re.Pattern[str]] = list(_DEFAULT_REDACT_PATTERNS)
    if extra_patterns:
        for raw in extra_patterns:
            try:
                patterns.append(re.compile(raw, re.IGNORECASE))
            except re.error:
                continue

    safe_payload = _make_json_safe(payload)
    redacted = _redact_value(safe_payload, patterns)
    redacted = _maybe_redact_media(redacted, max_record_bytes)
    # Final text truncation for any remaining long strings.
    redacted = _redact_value(redacted, patterns)
    return _truncate_payload(redacted, max_record_bytes)


def _truncate_payload(value: Any, max_bytes: int) -> Any:
    """Recursively truncate strings so the overall record stays bounded."""
    if isinstance(value, str):
        return _maybe_truncate_text(value, max_bytes)
    if isinstance(value, dict):
        return {k: _truncate_payload(v, max_bytes) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_payload(item, max_bytes) for item in value]
    return value
