# -*- coding: utf-8 -*-
"""Tests for trajectory collection in ToolCoordinator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.agent_context import (
    set_current_agent_id,
    set_current_session_id,
    set_current_trace_id,
)
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse
from qwenpaw.tool_calls._coordinator import ToolCoordinator
from qwenpaw.trajectory import register_trajectory_service, TrajectoryService
from qwenpaw.trajectory.models import TrajectoryConfig


@pytest.fixture
async def service(tmp_path: Path):
    svc = TrajectoryService(
        tmp_path,
        config=TrajectoryConfig(enabled=True),
    )
    await svc.start()
    yield svc


@pytest.mark.asyncio
async def test_records_tool_execution(service: TrajectoryService, tmp_path: Path):
    register_trajectory_service("agent-tool", service)
    set_current_agent_id("agent-tool")
    set_current_session_id("session-tool")
    set_current_trace_id("trace-tool")

    coordinator = ToolCoordinator()

    async def handler(tool_call):
        yield ToolResponse(
            content=[TextBlock(type="text", text="done")],
            id=tool_call.id,
        )

    tool_call = SimpleNamespace(id="tc-1", name="read_file")
    results = []
    async for item in coordinator.execute(
        tool_call=tool_call,
        next_handler=handler,
        session_id="session-tool",
        agent_id="agent-tool",
        root_session_id="session-tool",
    ):
        results.append(item)

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-tool.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    tool_events = [e for e in events if e["event_type"] == "tool_execution"]
    assert len(tool_events) == 1
    assert tool_events[0]["payload"]["tool_name"] == "read_file"
    assert tool_events[0]["payload"]["tool_call_id"] == "tc-1"


@pytest.mark.asyncio
async def test_records_tool_input_arguments(
    service: TrajectoryService,
    tmp_path: Path,
):
    """TOOL_EXECUTION must persist the tool call's input arguments."""
    register_trajectory_service("agent-tool-in", service)
    set_current_agent_id("agent-tool-in")
    set_current_session_id("session-tool-in")
    set_current_trace_id("trace-tool-in")

    coordinator = ToolCoordinator()

    async def handler(tool_call):
        yield ToolResponse(
            content=[TextBlock(type="text", text="ok")],
            id=tool_call.id,
        )

    tool_call = SimpleNamespace(
        id="tc-arg",
        name="read_file",
        input={"path": "/tmp/test.txt", "encoding": "utf-8"},
    )
    async for _ in coordinator.execute(
        tool_call=tool_call,
        next_handler=handler,
        session_id="session-tool-in",
        agent_id="agent-tool-in",
        root_session_id="session-tool-in",
    ):
        pass

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-tool-in.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    tool_events = [e for e in events if e["event_type"] == "tool_execution"]
    assert len(tool_events) == 1
    assert tool_events[0]["payload"]["input"] == {
        "path": "/tmp/test.txt",
        "encoding": "utf-8",
    }


@pytest.mark.asyncio
async def test_records_tool_input_none_when_missing(
    service: TrajectoryService,
    tmp_path: Path,
):
    """When the tool_call has no ``input`` attribute, payload['input'] is None."""
    register_trajectory_service("agent-tool-noin", service)
    set_current_agent_id("agent-tool-noin")
    set_current_session_id("session-tool-noin")
    set_current_trace_id("trace-tool-noin")

    coordinator = ToolCoordinator()

    async def handler(tool_call):
        yield ToolResponse(
            content=[TextBlock(type="text", text="ok")],
            id=tool_call.id,
        )

    tool_call = SimpleNamespace(id="tc-n", name="noop")
    async for _ in coordinator.execute(
        tool_call=tool_call,
        next_handler=handler,
        session_id="session-tool-noin",
        agent_id="agent-tool-noin",
        root_session_id="session-tool-noin",
    ):
        pass

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-tool-noin.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    tool_event = next(e for e in events if e["event_type"] == "tool_execution")
    assert tool_event["payload"]["input"] is None


@pytest.mark.asyncio
async def test_records_tool_input_from_json_string(
    service: TrajectoryService,
    tmp_path: Path,
):
    """agentscope 2.0 ``ToolCallBlock.input`` is a JSON string — parsed to dict.

    Closes the gap surfaced in functional review where the trajectory
    always recorded ``input=None`` because the parser required a dict.
    """
    register_trajectory_service("agent-tool-json", service)
    set_current_agent_id("agent-tool-json")
    set_current_session_id("session-tool-json")
    set_current_trace_id("trace-tool-json")

    coordinator = ToolCoordinator()

    async def handler(tool_call):
        yield ToolResponse(
            content=[TextBlock(type="text", text="ok")],
            id=tool_call.id,
        )

    tool_call = SimpleNamespace(
        id="tc-json",
        name="web_search",
        input='{"search_term": "hello world", "limit": 5}',
    )
    async for _ in coordinator.execute(
        tool_call=tool_call,
        next_handler=handler,
        session_id="session-tool-json",
        agent_id="agent-tool-json",
        root_session_id="session-tool-json",
    ):
        pass

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-tool-json.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    tool_event = next(e for e in events if e["event_type"] == "tool_execution")
    assert tool_event["payload"]["input"] == {
        "search_term": "hello world",
        "limit": 5,
    }


@pytest.mark.asyncio
async def test_records_tool_input_none_when_malformed_json(
    service: TrajectoryService,
    tmp_path: Path,
):
    """Malformed JSON string input is recorded as ``None``, not silently swallowed."""
    register_trajectory_service("agent-tool-bad", service)
    set_current_agent_id("agent-tool-bad")
    set_current_session_id("session-tool-bad")
    set_current_trace_id("trace-tool-bad")

    coordinator = ToolCoordinator()

    async def handler(tool_call):
        yield ToolResponse(
            content=[TextBlock(type="text", text="ok")],
            id=tool_call.id,
        )

    tool_call = SimpleNamespace(
        id="tc-bad",
        name="web_search",
        input="{not valid json",
    )
    async for _ in coordinator.execute(
        tool_call=tool_call,
        next_handler=handler,
        session_id="session-tool-bad",
        agent_id="agent-tool-bad",
        root_session_id="session-tool-bad",
    ):
        pass

    await service.stop()
    lines = (
        (tmp_path / "trajectory" / "session-tool-bad.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    tool_event = next(e for e in events if e["event_type"] == "tool_execution")
    # Unknown input must surface as an explicit None so consumers can
    # distinguish "no argument capture" from "argument was an empty
    # dict" — the latter would render as ``{}`` after JSON round-trip.
    assert tool_event["payload"]["input"] is None
