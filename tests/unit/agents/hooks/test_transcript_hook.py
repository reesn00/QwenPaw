# -*- coding: utf-8 -*-
"""TranscriptAppendHook isolation and enable-gate tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.agents.acp.meta import ACP_EPHEMERAL_META_KEY
from qwenpaw.config.config import TranscriptExportConfig
from qwenpaw.hooks.session.transcript_hook import TranscriptAppendHook

pytestmark = [pytest.mark.unit, pytest.mark.p1]


def _msg(text: str) -> Msg:
    return Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text=text)],
    )


def _ctx(
    tmp_path: Path,
    *,
    enabled: bool,
    ephemeral: bool = False,
    agent: object | None = ...,
    msgs: list | None = None,
):
    export_cfg = TranscriptExportConfig(enabled=enabled, path="transcripts")
    running = SimpleNamespace(transcript_export=export_cfg)
    agent_config = SimpleNamespace(running=running)

    if agent is ...:
        context = list(msgs or [_msg("hi")])
        state = SimpleNamespace(context=context)
        agent = SimpleNamespace(state=state)

    return SimpleNamespace(
        request=SimpleNamespace(
            request_context={ACP_EPHEMERAL_META_KEY: ephemeral},
            user_id="u1",
            channel="console",
        ),
        workspace=SimpleNamespace(workspace_dir=str(tmp_path)),
        workspace_dir=tmp_path,
        agent=agent,
        agent_config=agent_config,
        session_id="sess-hook-1",
        agent_id="default",
        mode_state={},
        extras={},
    )


async def test_disabled_writes_nothing(tmp_path: Path):
    ctx = _ctx(tmp_path, enabled=False)
    await TranscriptAppendHook().run(ctx)
    assert not (tmp_path / "transcripts").exists()


async def test_enabled_writes_jsonl(tmp_path: Path):
    ctx = _ctx(tmp_path, enabled=True, msgs=[_msg("hello export")])
    await TranscriptAppendHook().run(ctx)

    out_dir = tmp_path / "transcripts" / "console"
    files = list(out_dir.glob("*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "hello export" in content
    assert list(out_dir.glob("*.jsonl.ids"))


async def test_ephemeral_skips(tmp_path: Path):
    ctx = _ctx(tmp_path, enabled=True, ephemeral=True)
    await TranscriptAppendHook().run(ctx)
    assert not (tmp_path / "transcripts").exists()


async def test_no_agent_skips(tmp_path: Path):
    ctx = _ctx(tmp_path, enabled=True, agent=None)
    await TranscriptAppendHook().run(ctx)
    assert not (tmp_path / "transcripts").exists()


async def test_exception_is_swallowed(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path, enabled=True)

    def _boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        "qwenpaw.hooks.session.transcript_hook.export_new_messages",
        _boom,
    )
    # Must not raise.
    result = await TranscriptAppendHook().run(ctx)
    assert result is not None


async def test_idempotent_second_run(tmp_path: Path):
    msgs = [_msg("once")]
    ctx = _ctx(tmp_path, enabled=True, msgs=msgs)
    hook = TranscriptAppendHook()
    await hook.run(ctx)
    path = next((tmp_path / "transcripts" / "console").glob("*.jsonl"))
    first = path.read_bytes()
    await hook.run(ctx)
    assert path.read_bytes() == first
