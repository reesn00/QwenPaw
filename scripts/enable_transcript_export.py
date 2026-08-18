#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enable the session transcript export feature for an agent.

The transcript export pipeline is shipped wired-in:

* :class:`qwenpaw.agents.utils.transcript_export.export_new_messages`
  converts post-response messages into JSONL rows.
* :class:`qwenpaw.hooks.session.transcript_hook.TranscriptAppendHook`
  runs in the ``POST_RESPONSE`` phase (after ``session_save``) and is
  already registered in
  :class:`qwenpaw.app.workspace.bootstrap_factory.WorkspaceBootstrapFactory`.

The feature is therefore *active in code* but **off by default** for every
agent.  Flipping the toggle to ``true`` in the agent profile is the only
thing left to do — and that is what this script does, without requiring the
web app to be running.

Usage::

    # Enable transcript export for the default agent (uses defaults).
    python scripts/enable_transcript_export.py

    # Enable for a specific agent and write JSONL files to a custom folder.
    python scripts/enable_transcript_export.py \
        --agent-id dev \
        --path sessions/transcripts

    # Disable later (and keep the existing JSONL files on disk).
    python scripts/enable_transcript_export.py --disable

The script edits the agent's ``agent.json`` via the same
``load_agent_config`` / ``save_agent_config`` path used by every other
configuration command, so validation, atomic writes, and mtime-cache
invalidation all behave identically to ``qwenpaw init``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make ``python scripts/foo.py`` work without installing the project.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qwenpaw.config.config import (  # noqa: E402
    TranscriptExportConfig,
    load_agent_config,
    save_agent_config,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enable or disable append-only session transcript export "
            "(JSONL) for an agent."
        ),
    )
    parser.add_argument(
        "--agent-id",
        default="default",
        help="Agent profile to update (default: 'default').",
    )
    parser.add_argument(
        "--path",
        default=None,
        help=(
            "Workspace-relative directory for JSONL files "
            "(default: 'transcripts'). Ignored when --disable is set."
        ),
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Turn transcript export off instead of on.",
    )
    parser.add_argument(
        "--no-reasoning",
        action="store_true",
        help="Omit reasoning/thinking blocks from the JSONL rows.",
    )
    parser.add_argument(
        "--no-tool-calls",
        action="store_true",
        help="Omit tool/plugin call messages from the JSONL rows.",
    )
    parser.add_argument(
        "--no-tool-results",
        action="store_true",
        help="Omit tool/plugin result messages from the JSONL rows.",
    )
    parser.add_argument(
        "--max-tool-output-chars",
        type=int,
        default=None,
        help=(
            "Truncate tool output strings longer than this many characters "
            "(0 = no truncation, default: 50000)."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the resulting config without writing anything.",
    )
    return parser.parse_args(argv)


def _build_cfg(args: argparse.Namespace) -> TranscriptExportConfig:
    """Construct a TranscriptExportConfig honoring CLI overrides."""
    cfg = TranscriptExportConfig(enabled=not args.disable)
    if args.disable:
        # All toggles become irrelevant when disabled; keep defaults.
        return cfg

    if args.path is not None:
        cfg.path = args.path
    cfg.include_reasoning = not args.no_reasoning
    cfg.include_tool_calls = not args.no_tool_calls
    cfg.include_tool_results = not args.no_tool_results
    if args.max_tool_output_chars is not None:
        cfg.max_tool_output_chars = max(0, args.max_tool_output_chars)
    return cfg


def _print_status(label: str, cfg: TranscriptExportConfig) -> None:
    print(f"  {label}: {json.dumps(cfg.model_dump(), ensure_ascii=False)}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = _build_cfg(args)

    print(f"Loading agent profile: {args.agent_id}")
    try:
        agent_config = load_agent_config(args.agent_id)
    except Exception as exc:  # pylint: disable=broad-except
        print(
            f"Error: failed to load agent '{args.agent_id}': {exc}",
            file=sys.stderr,
        )
        return 1

    running = agent_config.running
    before = getattr(running, "transcript_export", None)

    print("Current transcript_export config:")
    _print_status("before", before or TranscriptExportConfig())
    print("New transcript_export config:")
    _print_status("after ", cfg)

    if args.show:
        print("--show requested; not writing changes.")
        return 0

    running.transcript_export = cfg
    try:
        save_agent_config(args.agent_id, agent_config)
    except Exception as exc:  # pylint: disable=broad-except
        print(
            f"Error: failed to save agent '{args.agent_id}': {exc}",
            file=sys.stderr,
        )
        return 1

    state = "ENABLED" if cfg.enabled else "DISABLED"
    location = f"{agent_config.workspace_dir or '<workspace>'}/{cfg.path}"
    print(f"\n✓ transcript_export {state} for agent '{args.agent_id}'.")
    if cfg.enabled:
        print(
            "  JSONL files will be appended at:\n"
            f"    {location}/<channel>/<session_id>.jsonl"
        )
        print(
            "  Restart the agent process so the bootstrap factory "
            "picks up the new config (workspace is loaded once at "
            "startup)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
