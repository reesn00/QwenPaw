# -*- coding: utf-8 -*-
"""High-level recorder used by integration points."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .buffer import TrajectoryBuffer
from .models import sanitize_payload, TrajectoryConfig, TrajectoryEvent, TrajectoryEventType

logger = logging.getLogger(__name__)


class TrajectoryRecorder:
    """Convenience wrapper around a ``TrajectoryBuffer``."""

    def __init__(
        self,
        buffer: TrajectoryBuffer,
        config: TrajectoryConfig,
    ) -> None:
        self._buffer = buffer
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def record(
        self,
        *,
        trace_id: str,
        event_type: TrajectoryEventType,
        payload: dict[str, Any],
        metadata: Optional[dict[str, Any]] = None,
        parent_span_id: Optional[str] = None,
        session_id: str = "",
        agent_id: str = "",
        user_id: str = "",
        channel: str = "",
        provider_id: str = "",
        model_name: str = "",
    ) -> Optional[TrajectoryEvent]:
        """Enqueue one trajectory event if recording is enabled."""
        if not self.enabled:
            return None
        try:
            safe_payload = sanitize_payload(
                payload,
                self._config.max_record_bytes,
                self._config.redact_patterns,
            )
            event = TrajectoryEvent(
                trace_id=trace_id,
                event_type=event_type,
                parent_span_id=parent_span_id,
                session_id=session_id,
                agent_id=agent_id,
                user_id=user_id,
                channel=channel,
                provider_id=provider_id,
                model_name=model_name,
                payload=safe_payload,
                metadata=metadata or {},
            )
            self._buffer.enqueue(event.model_dump(mode="json"))
            return event
        except Exception:
            logger.debug("trajectory: failed to record event", exc_info=True)
            return None


__all__ = ["TrajectoryRecorder"]
