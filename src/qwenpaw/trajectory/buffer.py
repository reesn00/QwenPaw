# -*- coding: utf-8 -*-
"""Async in-memory buffer with periodic JSONL flush for trajectory events.

Events are partitioned by ``session_id``: a single shared router queue feeds
per-session buckets, each with its own batch and JSONL file under
``<workspace>/<TRAJECTORY_DIR>/<session_id>.jsonl``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .storage import rotate_jsonl, save_data_sync

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL = 10  # seconds
_DEFAULT_MAX_QUEUE_SIZE = 10000
_BATCH_SIZE = 100
# Maximum length of the filename stem derived from a session_id.
_MAX_SESSION_FILENAME_LEN = 200
# Bucket key used when an event arrives with an empty / missing session_id
# so that records from different unspecified contexts still coexist on disk.
_FALLBACK_BUCKET_KEY = "__default__"
# Filename used for the fallback bucket.
_FALLBACK_BUCKET_NAME = "_default"

# Anything not in [A-Za-z0-9._-] gets squashed into a single underscore.
_SESSION_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_session_filename(session_id: str) -> str:
    """Sanitize ``session_id`` for use as a filename stem.

    Empty strings, traversal names (``.``, ``..``), or values that strip down
    to nothing fall back to :data:`_FALLBACK_BUCKET_NAME`. The result is
    always safe to concatenate with a ``.jsonl`` suffix.
    """
    if not session_id:
        return _FALLBACK_BUCKET_NAME
    safe = _SESSION_FILENAME_RE.sub("_", session_id).strip(".")
    if not safe or safe in {".", ".."}:
        return _FALLBACK_BUCKET_NAME
    return safe[:_MAX_SESSION_FILENAME_LEN]


@dataclass
class _SessionBucket:
    """Per-session state inside the routing buffer.

    ``lock`` serializes batch mutations within the bucket; a separate
    dict-level lock guards the bucket registry so the consumer can look up
    or create a bucket without blocking the entire pipeline.
    """

    session_id: str
    path: Path
    batch: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: bool = False


class TrajectoryBuffer:
    """Routing buffer that writes one JSONL file per session_id.

    A single ``asyncio.Queue`` feeds one consumer, which routes each event
    to a :class:`_SessionBucket` keyed by ``record["session_id"]``. Each
    bucket flushes independently when its batch fills up or the periodic
    flush tick fires. Retention and size rotation run once per bucket.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        flush_interval: int = _DEFAULT_FLUSH_INTERVAL,
        retention_days: int = 30,
        max_total_bytes: int = 0,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._flush_interval = flush_interval
        self._retention_days = retention_days
        self._max_total_bytes = max_total_bytes

        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_DEFAULT_MAX_QUEUE_SIZE,
        )
        self._buckets: dict[str, _SessionBucket] = {}
        self._buckets_lock = threading.Lock()
        self._consumer_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._stopped = False

    @property
    def base_dir(self) -> Path:
        """Directory under which per-session JSONL files live."""
        return self._base_dir

    def start(self) -> None:
        """Start consumer and flush tasks. Must be called from async context."""
        if self._consumer_task is not None:
            return
        self._stopped = False
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(),
            name="trajectory-consumer",
        )
        self._flush_task = asyncio.create_task(
            self._flush_loop(),
            name="trajectory-flush",
        )

    async def stop(self) -> None:
        """Drain queue, stop tasks, perform final flush and rotation."""
        self._stopped = True

        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        if self._consumer_task is not None:
            await self._queue.join()
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

        await self._flush_once(force=True)

    def enqueue(self, record: dict[str, Any]) -> None:
        """Fire-and-forget enqueue; never blocks the caller."""
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning(
                "trajectory: queue full, dropping event session=%s type=%s",
                record.get("session_id", ""),
                record.get("event_type", "unknown"),
            )

    def _resolve_bucket_key(self, record: dict[str, Any]) -> str:
        """Map a record to its bucket key.

        Empty / missing ``session_id`` collapses to a shared fallback bucket
        so unrelated unknown-context records do not spawn spurious files.
        """
        raw = record.get("session_id") or ""
        return raw if raw else _FALLBACK_BUCKET_KEY

    def _get_or_create_bucket(self, key: str) -> _SessionBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            return bucket
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                return bucket
            safe_name = _safe_session_filename(
                key if key != _FALLBACK_BUCKET_KEY else "",
            )
            bucket = _SessionBucket(
                session_id=key,
                path=self._base_dir / f"{safe_name}.jsonl",
            )
            self._buckets[key] = bucket
            return bucket

    def _snapshot_buckets(self) -> list[_SessionBucket]:
        with self._buckets_lock:
            return list(self._buckets.values())

    async def _consumer_loop(self) -> None:
        """Drain events into per-session batches."""
        try:
            while True:
                record = await self._queue.get()
                try:
                    key = self._resolve_bucket_key(record)
                    bucket = self._get_or_create_bucket(key)
                    async with bucket.lock:
                        bucket.batch.append(record)
                        bucket.dirty = True
                        if len(bucket.batch) >= _BATCH_SIZE:
                            await self._flush_bucket_locked(bucket)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            # Drain any remaining records per-bucket on shutdown.
            for bucket in self._snapshot_buckets():
                async with bucket.lock:
                    await self._flush_bucket_locked(bucket)
            raise

    async def _flush_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self._flush_once()
                except Exception:
                    logger.exception("trajectory: error during periodic flush")
        except asyncio.CancelledError:
            pass

    async def _flush_once(self, force: bool = False) -> None:
        for bucket in self._snapshot_buckets():
            async with bucket.lock:
                if bucket.batch or force:
                    await self._flush_bucket_locked(bucket)
            try:
                await asyncio.to_thread(
                    rotate_jsonl,
                    bucket.path,
                    self._retention_days,
                    self._max_total_bytes,
                )
            except Exception:
                logger.debug(
                    "trajectory: rotation failed for %s",
                    bucket.path,
                    exc_info=True,
                )

    async def _flush_bucket_locked(self, bucket: _SessionBucket) -> None:
        """Flush the current batch to disk. Caller must hold ``bucket.lock``."""
        if not bucket.batch:
            return
        snapshot = copy.deepcopy(bucket.batch)
        bucket.batch = []
        ok = await asyncio.to_thread(save_data_sync, bucket.path, snapshot)
        if ok:
            bucket.dirty = False
        else:
            # Re-queue the snapshot on failure so a later flush retries.
            bucket.batch.extend(snapshot)
            bucket.dirty = True


__all__ = ["TrajectoryBuffer", "_safe_session_filename"]