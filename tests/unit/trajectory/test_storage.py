# -*- coding: utf-8 -*-
"""Tests for trajectory JSONL storage helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.trajectory.storage import append_jsonl, rotate_jsonl


@pytest.mark.asyncio
async def test_append_jsonl_creates_file(tmp_path: Path):
    path = tmp_path / "trajectory.jsonl"
    ok = await append_jsonl(path, [{"event_type": "model_request"}])
    assert ok is True
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "model_request"


@pytest.mark.asyncio
async def test_append_jsonl_appends(tmp_path: Path):
    path = tmp_path / "trajectory.jsonl"
    await append_jsonl(path, [{"a": 1}])
    await append_jsonl(path, [{"a": 2}])
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["a"] == 2


@pytest.mark.asyncio
async def test_rotate_by_age_removes_old_files(tmp_path: Path):
    path = tmp_path / "trajectory.jsonl"
    old = tmp_path / "trajectory.2024-01-01.jsonl"
    old.write_text("{}")
    # Set mtime to long ago.
    import os
    import time

    os.utime(old, (time.time() - 86400 * 60, time.time() - 86400 * 60))
    rotate_jsonl(path, retention_days=30, max_total_bytes=0)
    assert not old.exists()
