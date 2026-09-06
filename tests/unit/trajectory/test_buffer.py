# -*- coding: utf-8 -*-
"""Tests for trajectory async buffer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.trajectory.buffer import TrajectoryBuffer, _safe_session_filename


@pytest.mark.asyncio
async def test_buffer_flushes_to_disk(tmp_path: Path):
    base_dir = tmp_path / "trajectory"
    buffer = TrajectoryBuffer(base_dir, flush_interval=1)
    buffer.start()
    buffer.enqueue(
        {
            "session_id": "s-1",
            "event_type": "model_request",
        },
    )
    await buffer.stop()

    expected = base_dir / "s-1.jsonl"
    assert expected.exists()
    lines = expected.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "model_request"


@pytest.mark.asyncio
async def test_buffer_force_flush_on_stop(tmp_path: Path):
    base_dir = tmp_path / "trajectory"
    buffer = TrajectoryBuffer(base_dir, flush_interval=60)
    buffer.start()
    for i in range(5):
        buffer.enqueue({"session_id": "s-1", "i": i})
    await buffer.stop()

    expected = base_dir / "s-1.jsonl"
    lines = expected.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5


@pytest.mark.asyncio
async def test_buffer_routes_events_by_session_id(tmp_path: Path):
    """Events with different session_id land in different files."""
    base_dir = tmp_path / "trajectory"
    buffer = TrajectoryBuffer(base_dir, flush_interval=1)
    buffer.start()

    buffer.enqueue({"session_id": "alpha", "i": 0})
    buffer.enqueue({"session_id": "beta", "i": 1})
    buffer.enqueue({"session_id": "alpha", "i": 2})
    buffer.enqueue({"session_id": "beta", "i": 3})

    await buffer.stop()

    alpha_lines = (base_dir / "alpha.jsonl").read_text(encoding="utf-8").strip().split("\n")
    beta_lines = (base_dir / "beta.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(alpha_lines) == 2
    assert len(beta_lines) == 2
    assert json.loads(alpha_lines[0])["i"] == 0
    assert json.loads(alpha_lines[1])["i"] == 2
    assert json.loads(beta_lines[0])["i"] == 1
    assert json.loads(beta_lines[1])["i"] == 3


@pytest.mark.asyncio
async def test_buffer_empty_session_id_uses_fallback(tmp_path: Path):
    """Events with no session_id collapse into a single fallback file."""
    base_dir = tmp_path / "trajectory"
    buffer = TrajectoryBuffer(base_dir, flush_interval=1)
    buffer.start()

    buffer.enqueue({"event_type": "a"})
    buffer.enqueue({"event_type": "b", "session_id": ""})
    buffer.enqueue({"event_type": "c", "session_id": None})

    await buffer.stop()

    fallback = base_dir / "_default.jsonl"
    assert fallback.exists()
    lines = fallback.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_buffer_sanitizes_session_filename(tmp_path: Path):
    """Unsafe characters in session_id collapse to underscores in the filename."""
    base_dir = tmp_path / "trajectory"
    buffer = TrajectoryBuffer(base_dir, flush_interval=1)
    buffer.start()

    buffer.enqueue({"session_id": "abc/../def", "i": 0})

    await buffer.stop()

    # ``abc/../def`` collapses to ``abc_.._def`` (slashes replaced with
    # underscores, path traversal impossible because path separators get
    # rewritten into filename-safe characters).
    candidate = base_dir / "abc_.._def.jsonl"
    assert candidate.exists()
    # Nothing landed in a literal ``def.jsonl`` (which would mean the
    # traversal actually traversed out of base_dir).
    assert not (base_dir / "def.jsonl").exists()
    # And nothing landed outside base_dir either.
    assert not (tmp_path / "def.jsonl").exists()
    # And the only file under base_dir is the sanitized one.
    assert sorted(p.name for p in base_dir.iterdir()) == ["abc_.._def.jsonl"]


def test_safe_session_filename_normalizes_unsafe_chars():
    assert _safe_session_filename("abc/def") == "abc_def"
    assert _safe_session_filename("a b*c") == "a_b_c"
    assert _safe_session_filename("...trailing...") == "trailing"
    assert _safe_session_filename("") == "_default"
    assert _safe_session_filename(".") == "_default"
    assert _safe_session_filename("..") == "_default"
    # Two Chinese characters each collapse into one underscore.
    assert _safe_session_filename("中文.session") == "__.session"


def test_safe_session_filename_truncates_long_input():
    long_id = "a" * 500
    assert len(_safe_session_filename(long_id)) == _MAX_SESSION_FILENAME_LEN_FROM_TEST()
    # Filename-safe chars survive; trailing length is bounded.
    assert _safe_session_filename(long_id).endswith("a" * 50)


def _MAX_SESSION_FILENAME_LEN_FROM_TEST() -> int:
    from qwenpaw.trajectory.buffer import _MAX_SESSION_FILENAME_LEN

    return _MAX_SESSION_FILENAME_LEN