# -*- coding: utf-8 -*-
"""Tests for trajectory collection in TokenRecordingModelWrapper."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.app.agent_context import (
    set_current_agent_id,
    set_current_session_id,
    set_current_trace_id,
)
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper
from qwenpaw.trajectory import (
    register_trajectory_service,
    TrajectoryService,
)
from qwenpaw.trajectory.models import TrajectoryConfig


class FakeChatModel:
    def __init__(self, response: Any):
        self._response = response
        self.model = "fake-model"
        self.credential = None
        self.parameters = type("Parameters", (), {})()
        self.stream = True
        self.context_size = 32768

    async def __call__(self, **kwargs):
        return self._response

    async def generate_structured_output(self, *args, **kwargs):
        return self._response


@pytest.fixture
async def service(tmp_path: Path):
    svc = TrajectoryService(
        tmp_path,
        config=TrajectoryConfig(enabled=True),
    )
    await svc.start()
    yield svc


@pytest.mark.asyncio
async def test_records_model_request_and_response(service: TrajectoryService, tmp_path: Path):
    register_trajectory_service("agent-mw", service)
    set_current_agent_id("agent-mw")
    set_current_session_id("session-mw")
    set_current_trace_id("trace-mw")

    response = SimpleNamespace(
        text="hello",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
        ),
        finish_reason="stop",
    )
    fake = FakeChatModel(response)
    wrapper = TokenRecordingModelWrapper("provider-1", fake)

    result = await wrapper(messages=[{"role": "user", "content": "hi"}])
    await service.stop()

    assert result is response
    lines = (
        (tmp_path / "trajectory" / "session-mw.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    types = [e["event_type"] for e in events]
    assert "model_request" in types
    assert "model_response" in types

    request_event = next(e for e in events if e["event_type"] == "model_request")
    assert request_event["trace_id"] == "trace-mw"
    assert request_event["provider_id"] == "provider-1"
    assert request_event["model_name"] == "fake-model"

    response_event = next(e for e in events if e["event_type"] == "model_response")
    assert response_event["parent_span_id"] == request_event["span_id"]


@pytest.mark.asyncio
async def test_records_tool_call_request(service: TrajectoryService, tmp_path: Path):
    register_trajectory_service("agent-mw-tc", service)
    set_current_agent_id("agent-mw-tc")
    set_current_session_id("session-mw-tc")
    set_current_trace_id("trace-mw-tc")

    response = SimpleNamespace(
        text="",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
        ),
        finish_reason="stop",
        tool_calls=[
            {
                "id": "tc-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test.txt"}',
                },
            },
        ],
    )
    fake = FakeChatModel(response)
    wrapper = TokenRecordingModelWrapper("provider-1", fake)

    result = await wrapper(messages=[{"role": "user", "content": "read it"}])
    await service.stop()

    assert result is response
    lines = (
        (tmp_path / "trajectory" / "session-mw-tc.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    types = [e["event_type"] for e in events]
    assert "tool_call_request" in types

    tool_call_event = next(e for e in events if e["event_type"] == "tool_call_request")
    response_event = next(e for e in events if e["event_type"] == "model_response")
    assert tool_call_event["parent_span_id"] == response_event["span_id"]
    assert tool_call_event["payload"]["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_structured_output_payload_normalizes_messages(
    service: TrajectoryService,
    tmp_path: Path,
):
    """MODEL_REQUEST from ``generate_structured_output`` exposes a normalized payload.

    Asserts that messages / tools / tool_choice surface at top level when
    provided via kwargs, and that ``response_schema`` is captured via
    ``model_json_schema`` when available.
    """
    from qwenpaw.token_usage.model_wrapper import (
        _structured_output_payload,
    )

    class FakeSchema:
        def model_json_schema(self):
            return {"type": "object", "properties": {"x": {"type": "integer"}}}

    payload = _structured_output_payload(
        (),
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "read_file"}],
            "tool_choice": "auto",
            "response_schema": FakeSchema(),
            "temperature": 0.7,
        },
    )
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"] == [{"name": "read_file"}]
    assert payload["tool_choice"] == "auto"
    assert payload["response_schema"] == {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }
    # ``temperature`` is not a known kwarg and must land in extra_kwargs
    assert payload["extra_kwargs"] == {"temperature": 0.7}


@pytest.mark.asyncio
async def test_structured_output_payload_positional_messages(
    service: TrajectoryService,
    tmp_path: Path,
):
    """When messages come as the first positional arg, they surface as ``messages``."""
    from qwenpaw.token_usage.model_wrapper import (
        _structured_output_payload,
    )

    payload = _structured_output_payload(
        ([{"role": "user", "content": "hi"}], "extra-positional"),
        {},
    )
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    # The second positional arg is preserved under ``args``
    assert payload["args"] == ["extra-positional"]


@pytest.mark.asyncio
async def test_structured_output_records_normalized_request(
    service: TrajectoryService,
    tmp_path: Path,
):
    """End-to-end: generate_structured_output writes a normalized payload."""
    register_trajectory_service("agent-mw-so", service)
    set_current_agent_id("agent-mw-so")
    set_current_session_id("session-mw-so")
    set_current_trace_id("trace-mw-so")

    class FakeSchema:
        def model_json_schema(self):
            return {"type": "object"}

    response = SimpleNamespace(
        text="{}",
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        finish_reason="stop",
    )
    fake = FakeChatModel(response)
    wrapper = TokenRecordingModelWrapper("provider-1", fake)

    result = await wrapper.generate_structured_output(
        messages=[{"role": "user", "content": "list it"}],
        response_schema=FakeSchema(),
    )
    await service.stop()

    assert result is response
    lines = (
        (tmp_path / "trajectory" / "session-mw-so.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    request_event = next(e for e in events if e["event_type"] == "model_request")
    assert request_event["payload"]["messages"] == [
        {"role": "user", "content": "list it"},
    ]
    assert request_event["payload"]["response_schema"] == {"type": "object"}
    # No opaque ``args`` blob anymore for the canonical kwargs path
    assert "args" not in request_event["payload"]


@pytest.mark.asyncio
async def test_records_response_content_from_blocks(
    service: TrajectoryService,
    tmp_path: Path,
):
    """agentscope 2.0 blocks structure: model_response preserves full content.

    Verifies the contract spelled out in DESIGN.md §8.5:

    * ``model_response.payload.content`` carries the ordered block list
      (``ThinkingBlock`` + ``TextBlock`` + ``ToolCallBlock``).
    * ``model_response.payload.finished_reason`` uses the 2.0 spelling.
    * A separate ``THINKING`` event is written so consumers that key
      off the typed event still get the reasoning chain.
    * A separate ``TOOL_CALL_REQUEST`` event is written with the
      OpenAI-compatible shape (``id`` / ``function.name`` /
      ``function.arguments`` string).
    """
    from agentscope.message import (
        TextBlock,
        ThinkingBlock,
        ToolCallBlock,
    )
    from agentscope.model._model_response import (
        ChatResponse,
        FinishedReason,
    )
    from agentscope.model._model_usage import ChatUsage

    register_trajectory_service("agent-blocks", service)
    set_current_agent_id("agent-blocks")
    set_current_session_id("session-blocks")
    set_current_trace_id("trace-blocks")

    response = ChatResponse(
        content=[
            ThinkingBlock(type="thinking", thinking="let me reason..."),
            TextBlock(type="text", text="the answer is 42"),
            ToolCallBlock(
                id="tc-blocks",
                name="read_file",
                input='{"path": "/tmp/x"}',
            ),
        ],
        is_last=True,
        usage=ChatUsage(
            input_tokens=10,
            output_tokens=20,
            time=0.5,
        ),
        finished_reason=FinishedReason.COMPLETED,
    )
    fake = FakeChatModel(response)
    wrapper = TokenRecordingModelWrapper("provider-1", fake)

    await wrapper(messages=[{"role": "user", "content": "q"}])
    await service.stop()

    events = [
        json.loads(line)
        for line in (
            (tmp_path / "trajectory" / "session-blocks.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .split("\n")
        )
    ]

    resp_event = next(e for e in events if e["event_type"] == "model_response")
    content_blocks = resp_event["payload"]["content"]
    assert isinstance(content_blocks, list)
    block_types = [b.get("type") for b in content_blocks]
    assert block_types == ["thinking", "text", "tool_call"]
    # Full text/thinking content preserved (no truncation expected here)
    thinking_block = next(b for b in content_blocks if b["type"] == "thinking")
    text_block = next(b for b in content_blocks if b["type"] == "text")
    tool_block = next(b for b in content_blocks if b["type"] == "tool_call")
    assert thinking_block["thinking"] == "let me reason..."
    assert text_block["text"] == "the answer is 42"
    assert tool_block["name"] == "read_file"
    assert tool_block["input"] == '{"path": "/tmp/x"}'
    # Usage + 2.0 finished_reason spelling
    assert resp_event["payload"]["usage"]["input_tokens"] == 10
    assert resp_event["payload"]["usage"]["output_tokens"] == 20
    assert resp_event["payload"]["finished_reason"] == "completed"

    thinking_events = [e for e in events if e["event_type"] == "thinking"]
    assert len(thinking_events) == 1
    assert thinking_events[0]["payload"]["thinking"] == "let me reason..."
    # THINKING is parented on MODEL_RESPONSE, not MODEL_REQUEST
    assert thinking_events[0]["parent_span_id"] == resp_event["span_id"]

    tc_events = [e for e in events if e["event_type"] == "tool_call_request"]
    assert len(tc_events) == 1
    assert tc_events[0]["payload"]["tool_calls"][0]["id"] == "tc-blocks"
    assert (
        tc_events[0]["payload"]["tool_calls"][0]["function"]["name"]
        == "read_file"
    )
    # Arguments are kept as a JSON string — OpenAI-compatible shape.
    assert (
        tc_events[0]["payload"]["tool_calls"][0]["function"]["arguments"]
        == '{"path": "/tmp/x"}'
    )
    assert tc_events[0]["parent_span_id"] == resp_event["span_id"]
