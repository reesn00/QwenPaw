# -*- coding: utf-8 -*-
"""Tests for trajectory collection in Envelope."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qwenpaw.app.agent_context import (
    set_current_agent_id,
    set_current_session_id,
    set_current_trace_id,
)
from qwenpaw.runtime.envelope import Envelope
from qwenpaw.trajectory import register_trajectory_service, TrajectoryService
from qwenpaw.trajectory.models import TrajectoryConfig


@pytest.fixture
async def service(tmp_path: Path):
    svc = TrajectoryService(
        tmp_path,
        config=TrajectoryConfig(enabled=True),
    )
    svc.start()
    yield svc


@pytest.mark.asyncio
async def test_records_final_reply(service: TrajectoryService, tmp_path: Path):
    register_trajectory_service("agent-env", service)
    set_current_agent_id("agent-env")
    set_current_session_id("session-env")
    set_current_trace_id("trace-env")

    envelope = Envelope(session_id="session-env")
    envelope._message_started = True
    envelope._completed_message.content = [
        type("TextContent", (), {
            "type": "text",
            "text": "hello",
            "model_dump": lambda mode: {"type": "text", "text": "hello"},
        })(),
    ]
    envelope._response.usage = {"input_tokens": 10, "output_tokens": 5}

    async for _ in envelope.finalize():
        pass

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-env.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    final_events = [e for e in events if e["event_type"] == "final_reply"]
    assert len(final_events) == 1
    assert final_events[0]["trace_id"] == "trace-env"
    assert final_events[0]["metadata"]["usage"]["output_tokens"] == 5
