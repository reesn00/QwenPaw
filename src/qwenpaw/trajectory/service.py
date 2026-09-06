# -*- coding: utf-8 -*-
"""Per-workspace trajectory service with global registry."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from ..constant import (
    TRAJECTORY_DIR,
    TRAJECTORY_ENABLED,
    TRAJECTORY_FLUSH_INTERVAL_SECONDS,
    TRAJECTORY_MAX_RECORD_BYTES,
    TRAJECTORY_RETENTION_DAYS,
)
from .buffer import TrajectoryBuffer
from .models import TrajectoryConfig
from .recorder import TrajectoryRecorder

logger = logging.getLogger(__name__)


class TrajectoryService:
    """Owns the trajectory buffer and recorder for one workspace.

    Events are partitioned by ``session_id`` and persisted as one JSONL
    file per session under :attr:`base_dir`.
    """

    def __init__(
        self,
        workspace_dir: Path,
        config: Optional[TrajectoryConfig] = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.config = config or TrajectoryConfig()
        self.base_dir = self.workspace_dir / TRAJECTORY_DIR
        self._buffer = TrajectoryBuffer(
            self.base_dir,
            flush_interval=self.config.flush_interval_seconds,
            retention_days=self.config.retention_days,
        )
        self.recorder = TrajectoryRecorder(self._buffer, self.config)

    async def start(self) -> None:
        if not self.recorder.enabled:
            return
        # Eagerly create the directory so the per-session JSONL files have
        # a stable parent for rotation even before the first event lands.
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # ``_buffer.start`` uses ``asyncio.create_task`` to launch the
        # consumer / flush loops, which requires a running event loop on
        # the current thread. ``ServiceManager._run_start_method`` calls
        # sync ``start`` methods via ``run_sync_io`` (worker thread); an
        # async ``start`` keeps us on the main loop where ``create_task``
        # is valid.
        self._buffer.start()
        logger.debug("trajectory: started for %s", self.workspace_dir)

    async def stop(self) -> None:
        await self._buffer.stop()
        logger.debug("trajectory: stopped for %s", self.workspace_dir)

    @property
    def path(self) -> Path:
        """Directory containing one ``<session_id>.jsonl`` per session."""
        return self._buffer.base_dir


# Global registry keyed by agent_id. Workspaces register themselves on start.
_registry: dict[str, TrajectoryService] = {}
_registry_lock = threading.Lock()


def register_trajectory_service(agent_id: str, service: TrajectoryService) -> None:
    with _registry_lock:
        _registry[agent_id] = service


def unregister_trajectory_service(agent_id: str) -> None:
    with _registry_lock:
        _registry.pop(agent_id, None)


def get_trajectory_service(agent_id: str) -> Optional[TrajectoryService]:
    """Return the trajectory service for *agent_id*, if any."""
    with _registry_lock:
        return _registry.get(agent_id)


def build_trajectory_config(
    agent_config: Optional[Any] = None,
) -> TrajectoryConfig:
    """Build config from agent config, falling back to env defaults."""
    from ..config.config import AgentsRunningConfig

    raw: Optional[dict] = None
    if agent_config is not None:
        running = getattr(agent_config, "running", None)
        if isinstance(running, AgentsRunningConfig):
            raw = getattr(running, "trajectory_config", None)
        elif isinstance(running, dict):
            raw = running.get("trajectory_config")
    if isinstance(raw, TrajectoryConfig):
        return raw
    if isinstance(raw, dict):
        return TrajectoryConfig.model_validate(raw)
    return TrajectoryConfig(
        enabled=TRAJECTORY_ENABLED,
        max_record_bytes=TRAJECTORY_MAX_RECORD_BYTES,
        retention_days=TRAJECTORY_RETENTION_DAYS,
        flush_interval_seconds=TRAJECTORY_FLUSH_INTERVAL_SECONDS,
    )


__all__ = [
    "TrajectoryService",
    "get_trajectory_service",
    "register_trajectory_service",
    "unregister_trajectory_service",
    "build_trajectory_config",
]
