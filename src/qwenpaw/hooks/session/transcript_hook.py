# -*- coding: utf-8 -*-
"""Append-only transcript export lifecycle hook.

Runs at ``POST_RESPONSE`` after :class:`SessionSaveHook`. When
``running.transcript_export.enabled`` is true, newly observed messages in
``agent.state.context`` are appended to a per-session JSONL file.

Best-effort only: every failure is logged and swallowed so the main request
path (SSE finalize, session save) is never affected.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..base import LifecycleHook
from .session_hook import _is_ephemeral_request
from ...agents.utils.transcript_export import export_new_messages
from ...runtime.hooks import HookContext, HookResult
from ...runtime.phases import Phase

logger = logging.getLogger(__name__)

# Serialize appends for the same session path across concurrent requests.
_PATH_LOCKS: dict[str, asyncio.Lock] = {}
_PATH_LOCKS_GUARD = asyncio.Lock()


async def _lock_for(key: str) -> asyncio.Lock:
    async with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _resolve_export_cfg(ctx: HookContext) -> Any | None:
    """Return TranscriptExportConfig when present; None means treat as off."""
    agent_config = getattr(ctx, "agent_config", None)
    running = getattr(agent_config, "running", None) if agent_config else None
    cfg = getattr(running, "transcript_export", None) if running else None
    if cfg is not None:
        return cfg

    # Fallback: load from disk only when agent_config was not injected.
    agent_id = getattr(ctx, "agent_id", None) or "default"
    try:
        from ...config.config import load_agent_config

        loaded = load_agent_config(agent_id)
        running = getattr(loaded, "running", None)
        return getattr(running, "transcript_export", None) if running else None
    except Exception:
        logger.debug(
            "transcript_append: failed to load agent config for %s",
            agent_id,
            exc_info=True,
        )
        return None


def _context_msgs(agent: Any) -> list[Any]:
    state = getattr(agent, "state", None)
    if state is None:
        return []
    context = getattr(state, "context", None)
    if not context:
        return []
    try:
        return list(context)
    except Exception:
        return []


class TranscriptAppendHook(LifecycleHook):
    """Append new conversation messages to a session JSONL transcript."""

    phase = Phase.POST_RESPONSE
    name = "transcript_append"
    priority = 95
    after = ("session_save",)

    async def run(self, ctx: HookContext) -> HookResult:
        # Never raise — registry does not swallow hook exceptions.
        try:
            return await self._run_impl(ctx)
        except Exception:
            logger.warning(
                "transcript_append: unexpected failure session=%s",
                getattr(ctx, "session_id", ""),
                exc_info=True,
            )
            return HookResult()

    async def _run_impl(self, ctx: HookContext) -> HookResult:
        if _is_ephemeral_request(ctx):
            return HookResult()

        cfg = _resolve_export_cfg(ctx)
        if cfg is None or not getattr(cfg, "enabled", False):
            return HookResult()

        agent = getattr(ctx, "agent", None)
        if agent is None:
            return HookResult()

        workspace_dir = getattr(ctx, "workspace_dir", None)
        if not workspace_dir:
            workspace = getattr(ctx, "workspace", None)
            workspace_dir = getattr(workspace, "workspace_dir", None)
        if not workspace_dir:
            return HookResult()

        msgs = _context_msgs(agent)
        if not msgs:
            return HookResult()

        request = getattr(ctx, "request", None)
        channel = (getattr(request, "channel", "") if request else "") or ""
        session_id = getattr(ctx, "session_id", "") or ""
        agent_id = getattr(ctx, "agent_id", "") or ""
        if not session_id:
            return HookResult()

        lock_key = f"{workspace_dir}|{channel}|{session_id}"
        lock = await _lock_for(lock_key)
        async with lock:
            written = await asyncio.to_thread(
                export_new_messages,
                msgs,
                cfg,
                workspace_dir=workspace_dir,
                session_id=session_id,
                agent_id=agent_id,
                channel=channel,
            )
        if written:
            logger.debug(
                "transcript_append: wrote %d row(s) session=%s",
                written,
                session_id,
            )
        return HookResult()


__all__ = ["TranscriptAppendHook"]
