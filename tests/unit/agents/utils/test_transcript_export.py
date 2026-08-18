# -*- coding: utf-8 -*-
"""Unit tests for transcript export helpers."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import (
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from qwenpaw.agents.utils.transcript_export import (
    export_new_messages,
    filter_new_msgs,
    load_exported_ids,
    resolve_transcript_paths,
    save_exported_ids,
    serialize_msgs,
)
from qwenpaw.config.config import TranscriptExportConfig

pytestmark = [pytest.mark.unit, pytest.mark.p1]


def test_resolve_rejects_path_traversal(tmp_path: Path):
    assert (
        resolve_transcript_paths(
            tmp_path,
            rel_path="../outside",
            channel="console",
            session_id="s1",
        )
        is None
    )


def test_resolve_builds_sanitized_paths(tmp_path: Path):
    paths = resolve_transcript_paths(
        tmp_path,
        rel_path="transcripts",
        channel="console",
        session_id="discord:dm:123",
    )
    assert paths is not None
    jsonl_path, ids_path = paths
    assert jsonl_path.parent == tmp_path / "transcripts" / "console"
    assert jsonl_path.name == "discord--dm--123.jsonl"
    assert ids_path.name == "discord--dm--123.jsonl.ids"


def test_sidecar_roundtrip(tmp_path: Path):
    ids_path = tmp_path / "s.jsonl.ids"
    save_exported_ids(ids_path, {"b", "a", ""})
    assert load_exported_ids(ids_path) == {"a", "b"}


def test_filter_new_msgs_skips_missing_and_exported():
    msgs = [
        SimpleNamespace(id="m1"),
        SimpleNamespace(id=None),
        SimpleNamespace(id="m2"),
        SimpleNamespace(id="m1"),
    ]
    out = filter_new_msgs(msgs, {"m1"})
    assert [m.id for m in out] == ["m2"]


def test_serialize_includes_reasoning_and_tool_call():
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="let me think"),
            TextBlock(type="text", text="hello"),
            ToolCallBlock(
                type="tool_call",
                id="call-1",
                name="execute_shell_command",
                input='{"command": "echo hi"}',
            ),
        ],
    )
    cfg = TranscriptExportConfig(enabled=True)
    rows = serialize_msgs([msg], cfg, session_id="s", agent_id="default")
    types = {row["type"] for row in rows}
    assert "reasoning" in types or any(
        "thinking" in json.dumps(row, ensure_ascii=False) for row in rows
    )
    assert any(row.get("msg_id") == msg.id for row in rows)


def test_serialize_filters_reasoning_when_disabled():
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="secret"),
            TextBlock(type="text", text="visible"),
        ],
    )
    cfg = TranscriptExportConfig(enabled=True, include_reasoning=False)
    rows = serialize_msgs([msg], cfg, session_id="s")
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "secret" not in dumped
    assert "visible" in dumped


def test_export_new_messages_append_and_idempotent(tmp_path: Path):
    cfg = TranscriptExportConfig(enabled=True, path="transcripts")
    user = Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text="hi")],
    )
    assistant = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="hello")],
    )

    n1 = export_new_messages(
        [user, assistant],
        cfg,
        workspace_dir=tmp_path,
        session_id="sess-1",
        agent_id="default",
        channel="console",
    )
    assert n1 > 0

    paths = resolve_transcript_paths(
        tmp_path,
        rel_path="transcripts",
        channel="console",
        session_id="sess-1",
    )
    assert paths is not None
    jsonl_path, ids_path = paths
    first_bytes = jsonl_path.read_bytes()
    assert ids_path.exists()

    n2 = export_new_messages(
        [user, assistant],
        cfg,
        workspace_dir=tmp_path,
        session_id="sess-1",
        agent_id="default",
        channel="console",
    )
    assert n2 == 0
    assert jsonl_path.read_bytes() == first_bytes

    # New message only appends the delta.
    extra = Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text="again")],
    )
    n3 = export_new_messages(
        [user, assistant, extra],
        cfg,
        workspace_dir=tmp_path,
        session_id="sess-1",
        agent_id="default",
        channel="console",
    )
    assert n3 > 0
    assert len(jsonl_path.read_bytes()) > len(first_bytes)


def test_export_disabled_writes_nothing(tmp_path: Path):
    cfg = TranscriptExportConfig(enabled=False)
    msg = Msg(
        name="user",
        role="user",
        content=[TextBlock(type="text", text="hi")],
    )
    n = export_new_messages(
        [msg],
        cfg,
        workspace_dir=tmp_path,
        session_id="sess-1",
        channel="console",
    )
    assert n == 0
    assert not (tmp_path / "transcripts").exists()


def test_tool_output_truncation():
    long_output = "x" * 100
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolResultBlock(
                type="tool_result",
                id="call-1",
                name="execute_shell_command",
                output=long_output,
            ),
        ],
    )
    cfg = TranscriptExportConfig(
        enabled=True,
        max_tool_output_chars=10,
    )
    rows = serialize_msgs([msg], cfg, session_id="s")
    assert rows
    # Find truncated output in content data blocks.
    found = False
    for row in rows:
        for block in row.get("content") or []:
            if not isinstance(block, dict):
                continue
            data = block.get("data") or {}
            if data.get("truncated") is True:
                assert len(data.get("output", "")) == 10
                assert data.get("original_output_chars") == 100
                found = True
    assert found
