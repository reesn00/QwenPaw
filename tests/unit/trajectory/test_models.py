# -*- coding: utf-8 -*-
"""Tests for trajectory data models and sanitization."""

from __future__ import annotations

import pytest

from qwenpaw.trajectory.models import (
    sanitize_payload,
    TrajectoryConfig,
    TrajectoryEvent,
    TrajectoryEventType,
)


def test_trajectory_event_serialization():
    event = TrajectoryEvent(
        trace_id="trace-1",
        event_type=TrajectoryEventType.MODEL_REQUEST,
        session_id="session-1",
        agent_id="agent-1",
        payload={"messages": [{"role": "user", "content": "hello"}]},
    )
    data = event.model_dump(mode="json")
    assert data["trace_id"] == "trace-1"
    assert data["event_type"] == "model_request"
    assert data["session_id"] == "session-1"
    assert "span_id" in data


def test_redaction_of_api_key():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "use api_key=super-secret-key please",
            },
        ],
        "headers": {"Authorization": "Bearer abc123"},
    }
    safe = sanitize_payload(payload, max_record_bytes=1024)
    assert "[redacted]" in safe["messages"][0]["content"]
    assert "[redacted]" in safe["headers"]["Authorization"]


def test_media_truncation():
    payload = {
        "messages": [
            {
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "data": "a" * 10_000,
                            "media_type": "image/png",
                        },
                    },
                ],
            },
        ],
    }
    safe = sanitize_payload(payload, max_record_bytes=1024)
    source = safe["messages"][0]["content"][0]["source"]
    assert "bytes media omitted" in source["data"]


def test_long_string_truncation():
    payload = {"text": "x" * 1_000_000}
    safe = sanitize_payload(payload, max_record_bytes=1024)
    assert "...[truncated]" in safe["text"]
    assert len(safe["text"].encode("utf-8")) <= 1024


def test_trajectory_config_defaults():
    cfg = TrajectoryConfig()
    assert cfg.enabled is True
    assert cfg.max_record_bytes == 256 * 1024
    assert cfg.retention_days == 30


def test_invalid_redact_pattern_is_ignored():
    payload = {"text": "hello"}
    safe = sanitize_payload(payload, max_record_bytes=1024, extra_patterns=["["])
    assert safe["text"] == "hello"
