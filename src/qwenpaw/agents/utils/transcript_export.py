# -*- coding: utf-8 -*-
"""Append-only conversation transcript export helpers.

Pure utilities used by :class:`TranscriptAppendHook`. Keep this module free of
Runtime / Workspace imports so it stays unit-testable in isolation.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from qwenpaw.app.chats.session import sanitize_filename
from qwenpaw.app.chats.utils import agentscope_msg_to_message
from qwenpaw.schemas import MessageType

logger = logging.getLogger(__name__)

TRANSCRIPT_SCHEMA_VERSION = 1

# Message types that map to the include_* config toggles.
_REASONING_TYPES = frozenset({MessageType.REASONING.value, "reasoning"})
_TOOL_CALL_TYPES = frozenset(
    {
        MessageType.PLUGIN_CALL.value,
        MessageType.FUNCTION_CALL.value,
        MessageType.MCP_TOOL_CALL.value,
        "plugin_call",
        "function_call",
        "mcp_tool_call",
        "tool_use",
        "tool_call",
    },
)
_TOOL_RESULT_TYPES = frozenset(
    {
        MessageType.PLUGIN_CALL_OUTPUT.value,
        MessageType.FUNCTION_CALL_OUTPUT.value,
        MessageType.MCP_TOOL_CALL_OUTPUT.value,
        "plugin_call_output",
        "function_call_output",
        "mcp_tool_call_output",
        "tool_result",
    },
)


def resolve_transcript_paths(
    workspace_dir: str | Path,
    *,
    rel_path: str,
    channel: str,
    session_id: str,
) -> tuple[Path, Path] | None:
    """Resolve JSONL + sidecar paths under *workspace_dir*.

    Returns ``None`` when *rel_path* would escape the workspace (path traversal)
    or when required identifiers are empty.
    """
    if not workspace_dir or not session_id:
        return None

    root = Path(workspace_dir).expanduser().resolve()
    rel = (rel_path or "transcripts").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        logger.warning(
            "transcript_export: rejected path %r (must be a relative path "
            "without '..')",
            rel_path,
        )
        return None

    base = (root / rel).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        logger.warning(
            "transcript_export: path %s escapes workspace %s",
            base,
            root,
        )
        return None

    safe_channel = sanitize_filename(channel or "unknown") or "unknown"
    safe_sid = sanitize_filename(session_id)
    if not safe_sid:
        return None

    jsonl_path = base / safe_channel / f"{safe_sid}.jsonl"
    ids_path = base / safe_channel / f"{safe_sid}.jsonl.ids"
    return jsonl_path, ids_path


def load_exported_ids(ids_path: Path) -> set[str]:
    """Load previously exported message ids from the sidecar file."""
    if not ids_path.exists():
        return set()
    try:
        raw = ids_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "transcript_export: failed to read ids sidecar %s: %s",
            ids_path,
            exc,
        )
        return set()
    if not raw.strip():
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: one id per line.
        return {line.strip() for line in raw.splitlines() if line.strip()}
    if isinstance(data, list):
        return {str(item) for item in data if item}
    if isinstance(data, dict):
        ids = data.get("ids")
        if isinstance(ids, list):
            return {str(item) for item in ids if item}
    return set()


def save_exported_ids(ids_path: Path, ids: Iterable[str]) -> None:
    """Atomically write the exported-id set to the sidecar file."""
    payload = {
        "v": TRANSCRIPT_SCHEMA_VERSION,
        "ids": sorted({str(i) for i in ids if i}),
    }
    ids_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(ids_path.parent),
            prefix=f".{ids_path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
            newline="\n",
        ) as handle:
            tmp_path = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, ids_path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def filter_new_msgs(msgs: Sequence[Any], exported: set[str]) -> list[Any]:
    """Return messages whose ``id`` is set and not yet in *exported*."""
    out: list[Any] = []
    for msg in msgs:
        msg_id = getattr(msg, "id", None)
        if not msg_id:
            continue
        msg_id_str = str(msg_id)
        if msg_id_str in exported:
            continue
        out.append(msg)
    return out


def _message_type_value(message: Any) -> str:
    mtype = getattr(message, "type", None)
    if mtype is None:
        return ""
    return str(getattr(mtype, "value", mtype))


def _should_include(message: Any, cfg: Any) -> bool:
    mtype = _message_type_value(message)
    if mtype in _REASONING_TYPES:
        return bool(getattr(cfg, "include_reasoning", True))
    if mtype in _TOOL_CALL_TYPES:
        return bool(getattr(cfg, "include_tool_calls", True))
    if mtype in _TOOL_RESULT_TYPES:
        return bool(getattr(cfg, "include_tool_results", True))
    return True


def _truncate_tool_output(row: dict[str, Any], max_chars: int) -> None:
    if max_chars <= 0:
        return
    content = row.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        output = data.get("output")
        if isinstance(output, str) and len(output) > max_chars:
            data["output"] = output[:max_chars]
            data["truncated"] = True
            data["original_output_chars"] = len(output)


def serialize_msgs(
    msgs: Sequence[Any],
    cfg: Any,
    *,
    session_id: str = "",
    agent_id: str = "",
    channel: str = "",
) -> list[dict[str, Any]]:
    """Convert AgentScope msgs into JSON-serializable transcript rows."""
    if not msgs:
        return []

    try:
        messages = agentscope_msg_to_message(list(msgs))
    except Exception:
        logger.warning(
            "transcript_export: agentscope_msg_to_message failed",
            exc_info=True,
        )
        return []

    # Map runtime Message back to source msg.id via metadata.original_id.
    now = datetime.now(timezone.utc).isoformat()
    max_chars = int(getattr(cfg, "max_tool_output_chars", 0) or 0)
    rows: list[dict[str, Any]] = []

    for message in messages:
        if not _should_include(message, cfg):
            continue
        meta = getattr(message, "metadata", None) or {}
        msg_id = ""
        if isinstance(meta, dict):
            msg_id = str(meta.get("original_id") or "")
        dumped = message.model_dump(mode="json")
        row = {
            "v": TRANSCRIPT_SCHEMA_VERSION,
            "ts": (
                meta.get("timestamp")
                if isinstance(meta, dict) and meta.get("timestamp")
                else now
            ),
            "session_id": session_id,
            "agent_id": agent_id,
            "channel": channel,
            "msg_id": msg_id,
            "role": dumped.get("role"),
            "type": dumped.get("type"),
            "content": dumped.get("content"),
            "status": dumped.get("status"),
            "metadata": dumped.get("metadata"),
        }
        _truncate_tool_output(row, max_chars)
        rows.append(row)
    return rows


def append_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Append *rows* as JSONL lines to *path* (creates parent dirs)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def export_new_messages(
    msgs: Sequence[Any],
    cfg: Any,
    *,
    workspace_dir: str | Path,
    session_id: str,
    agent_id: str = "",
    channel: str = "",
) -> int:
    """Export messages not yet recorded for this session.

    Returns the number of JSONL rows written. Safe to call repeatedly (idempotent
    w.r.t. message ids already present in the sidecar).
    """
    if not getattr(cfg, "enabled", False):
        return 0

    paths = resolve_transcript_paths(
        workspace_dir,
        rel_path=str(getattr(cfg, "path", "transcripts") or "transcripts"),
        channel=channel,
        session_id=session_id,
    )
    if paths is None:
        return 0
    jsonl_path, ids_path = paths

    exported = load_exported_ids(ids_path)
    new_msgs = filter_new_msgs(msgs, exported)
    if not new_msgs:
        return 0

    rows = serialize_msgs(
        new_msgs,
        cfg,
        session_id=session_id,
        agent_id=agent_id,
        channel=channel,
    )
    if not rows:
        # Still advance the cursor for msgs that produced zero rows after
        # filtering (e.g. reasoning disabled) so we don't rescan them forever.
        exported.update(
            str(getattr(m, "id", ""))
            for m in new_msgs
            if getattr(m, "id", None)
        )
        save_exported_ids(ids_path, exported)
        return 0

    append_jsonl(jsonl_path, rows)
    exported.update(
        str(getattr(m, "id", "")) for m in new_msgs if getattr(m, "id", None)
    )
    save_exported_ids(ids_path, exported)
    return len(rows)


__all__ = [
    "TRANSCRIPT_SCHEMA_VERSION",
    "append_jsonl",
    "export_new_messages",
    "filter_new_msgs",
    "load_exported_ids",
    "resolve_transcript_paths",
    "save_exported_ids",
    "serialize_msgs",
]
