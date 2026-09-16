# -*- coding: utf-8 -*-
"""Tests for trajectory collection in Runtime._record_turn_start.

Covers the post-build enrichment that surfaces provider_id / model_name /
agent_backend on the TURN_START event.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.agent_context import (
    set_current_agent_id,
    set_current_session_id,
    set_current_trace_id,
)
from qwenpaw.runtime.runtime import Runtime
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


def _ctx_with_agent(agent: object, agent_id: str = "default") -> object:
    """Build a minimal HookContext-shaped namespace for the helpers."""
    return SimpleNamespace(
        agent_id=agent_id,
        session_id="s",
        input_msgs=[],
        request=SimpleNamespace(user_id="u", channel="console"),
        workspace=None,
        agent=agent,
        agent_config=None,
    )


class _Inner:
    def __init__(self, provider_id: str, model: str) -> None:
        self._provider_id = provider_id
        self.model = model


class _RecordingWrapper:
    """Mimics ``TokenRecordingModelWrapper``."""

    def __init__(self, provider_id: str, model: str) -> None:
        self._provider_id = provider_id
        self.model = model


class _DoubleWrapped:
    """Chain ``_RecordingWrapper(_Inner(...))`` exposed via ``_inner``."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        # Deliberately no ``_provider_id`` here — drill-down should reach
        # the inner adapter.


def test_resolve_turn_model_from_recording_wrapper():
    agent = SimpleNamespace(model=_RecordingWrapper("anthropic", "claude-3-5-sonnet"))
    provider, model = Runtime._resolve_turn_model(_ctx_with_agent(agent))
    assert provider == "anthropic"
    assert model == "claude-3-5-sonnet"


def test_resolve_turn_model_drills_through_double_wrap():
    wrapper = _DoubleWrapped(_RecordingWrapper("dashscope", "qwen-plus"))
    # _RecordingWrapper on the outer layer — first match wins.
    agent = SimpleNamespace(model=wrapper)
    provider, model = Runtime._resolve_turn_model(_ctx_with_agent(agent))
    assert provider == "dashscope"
    assert model == "qwen-plus"


def test_resolve_turn_model_falls_back_to_active_model():
    # ``_provider_id`` and ``model`` both missing on the live model — fall
    # back to ``ctx.agent_config.active_model``.
    class _Empty:
        pass

    agent = SimpleNamespace(model=_Empty())
    ctx = _ctx_with_agent(agent)
    ctx.agent_config = SimpleNamespace(
        active_model=SimpleNamespace(provider_id="openai", model="gpt-4o"),
    )
    provider, model = Runtime._resolve_turn_model(ctx)
    assert provider == "openai"
    assert model == "gpt-4o"


def test_resolve_turn_model_partial_match_does_not_fall_back():
    """When the live model has ``model`` but no ``_provider_id``, return
    the partial match — do NOT silently fall back to active_model."""
    agent = SimpleNamespace(
        model=SimpleNamespace(model="orphan", _provider_id=None),
    )
    provider, model = Runtime._resolve_turn_model(_ctx_with_agent(agent))
    assert provider == ""
    assert model == "orphan"


def test_resolve_turn_model_returns_empty_when_unknown():
    agent = SimpleNamespace(model=None)
    ctx = _ctx_with_agent(agent)
    provider, model = Runtime._resolve_turn_model(ctx)
    assert provider == ""
    assert model == ""


def test_resolve_turn_backend_reads_workspace_config():
    ctx = _ctx_with_agent(None)
    ctx.workspace = SimpleNamespace(
        config=SimpleNamespace(backend="qwenpaw"),
    )
    assert Runtime._resolve_turn_backend(ctx) == "qwenpaw"


def test_resolve_turn_backend_returns_empty_without_config():
    ctx = _ctx_with_agent(None)
    assert Runtime._resolve_turn_backend(ctx) == ""


@pytest.mark.asyncio
async def test_record_turn_start_writes_provider_model_backend(
    service: TrajectoryService,
    tmp_path: Path,
):
    """End-to-end: _record_turn_start persists provider_id / model_name / backend."""
    register_trajectory_service("agent-rt", service)
    set_current_agent_id("agent-rt")
    set_current_session_id("session-rt")
    set_current_trace_id("trace-rt")

    runtime = Runtime(workspace=None, app_services=None)
    agent = SimpleNamespace(model=_RecordingWrapper("openai", "gpt-4o-mini"))
    ctx = _ctx_with_agent(agent, agent_id="agent-rt")
    # _record_turn_start reads ctx.session_id directly (not the contextvar)
    # so the per-session JSONL we read must match.
    ctx.session_id = "session-rt"
    ctx.workspace = SimpleNamespace(
        config=SimpleNamespace(backend="qwenpaw"),
    )

    runtime._record_turn_start(
        ctx,
        request=ctx.request,
        trace_id="trace-rt",
    )
    await service.stop()

    lines = (
        (tmp_path / "trajectory" / "session-rt.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .split("\n")
    )
    events = [json.loads(line) for line in lines]
    turn_events = [e for e in events if e["event_type"] == "turn_start"]
    assert len(turn_events) == 1
    event = turn_events[0]
    assert event["provider_id"] == "openai"
    assert event["model_name"] == "gpt-4o-mini"
    assert event["payload"]["agent_backend"] == "qwenpaw"