# -*- coding: utf-8 -*-
"""File I/O for trajectory JSONL with rotation."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Best-effort ISO timestamp parser."""
    try:
        # Python 3.11+ supports Z suffix directly.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def append_jsonl(path: Path, records: list[dict[str, Any]]) -> bool:
    """Append *records* as newline-delimited JSON to *path*.

    Returns True on success. Each record is written on its own line.
    """
    if not records:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record, ensure_ascii=False) + "\n" for record in records]
        async with aiofiles.open(path, mode="a", encoding="utf-8") as f:
            await f.writelines(lines)
        return True
    except OSError as exc:
        logger.warning("trajectory: failed to append to %s: %s", path, exc)
        return False


def _rotate_by_age(path: Path, retention_days: int) -> None:
    """Remove JSONL files in the same directory older than *retention_days*."""
    if retention_days <= 0:
        return
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
    directory = path.parent
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith(path.stem):
            continue
        try:
            mtime = entry.stat().st_mtime
            mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if mtime_dt < cutoff:
                entry.unlink()
                logger.info("trajectory: rotated old file %s", entry)
        except OSError as exc:
            logger.debug("trajectory: failed to stat/unlink %s: %s", entry, exc)


def _rotate_by_size(path: Path, max_total_bytes: int) -> None:
    """Keep the most recent *max_total_bytes* of matching JSONL files."""
    if max_total_bytes <= 0:
        return
    directory = path.parent
    try:
        entries = [
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.name.startswith(path.stem)
        ]
    except OSError:
        return
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    total = 0
    for entry in entries:
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        total += size
        if total > max_total_bytes:
            try:
                entry.unlink()
                logger.info("trajectory: rotated oversized file %s", entry)
            except OSError as exc:
                logger.debug("trajectory: failed to unlink %s: %s", entry, exc)


def rotate_jsonl(
    path: Path,
    retention_days: int,
    max_total_bytes: int = 0,
) -> None:
    """Apply age-based and optional size-based rotation."""
    _rotate_by_age(path, retention_days)
    if max_total_bytes > 0:
        _rotate_by_size(path, max_total_bytes)


def save_data_sync(path: Path, records: list[dict[str, Any]]) -> bool:
    """Synchronous append used by the async flush task via ``asyncio.to_thread``."""
    if not records:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record, ensure_ascii=False) + "\n" for record in records]
        with open(path, mode="a", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except OSError as exc:
        logger.warning("trajectory: failed to write %s: %s", path, exc)
        return False


__all__ = [
    "append_jsonl",
    "rotate_jsonl",
    "save_data_sync",
]
