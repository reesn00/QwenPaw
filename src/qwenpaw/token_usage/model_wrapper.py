# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

import json
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncGenerator, Literal

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from ..utils.model_response import safe_attr
from .buffer import _UsageEvent
from .manager import _usage_agent_id, get_token_usage_manager


def _get_trajectory_recorder() -> Any | None:
    """Return the active trajectory recorder for the current agent, if any."""
    try:
        from ..app.agent_context import get_current_agent_id
        from ..trajectory import get_trajectory_service

        service = get_trajectory_service(get_current_agent_id())
        if service is not None:
            return service.recorder
    except Exception:
        pass
    return None


def _request_payload(
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: Any,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build a serializable request payload."""
    payload: dict[str, Any] = {"messages": messages}
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if kwargs:
        payload["kwargs"] = kwargs
    return payload


# Fields commonly accepted by AgentScope ``generate_structured_output``. We
# surface them at the top level of MODEL_REQUEST so trajectory consumers see
# the same shape as the ``__call__`` path; anything else drops into
# ``extra_kwargs`` / ``args`` for completeness without leaking the entire raw
# ``*args, **kwargs`` blob.
_STRUCTURED_OUTPUT_KNOWN_KWARGS = frozenset(
    {
        "messages",
        "tools",
        "tool_choice",
        "response_schema",
        "structured_model",
    },
)


def _structured_output_payload(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort normalization of ``generate_structured_output`` arguments.

    AgentScope providers pass ``messages`` either as the first positional
    arg or as the ``messages`` kwarg; ``tools`` / ``tool_choice`` may
    also appear when the schema-aware call goes through the same code path.
    ``response_schema`` is dropped from the residual bag and serialized via
    ``model_json_schema()`` when available so the trajectory stays small.
    """
    payload: dict[str, Any] = {}
    messages = kwargs.get("messages")
    if messages is None and args and isinstance(args[0], list):
        messages = args[0]
    if messages is not None:
        payload["messages"] = messages

    if kwargs.get("tools") is not None:
        payload["tools"] = kwargs["tools"]
    if kwargs.get("tool_choice") is not None:
        payload["tool_choice"] = kwargs["tool_choice"]

    schema = kwargs.get("response_schema")
    if schema is not None:
        try:
            payload["response_schema"] = schema.model_json_schema()
        except Exception:
            try:
                payload["response_schema"] = str(schema)
            except Exception:
                pass

    extra_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key not in _STRUCTURED_OUTPUT_KNOWN_KWARGS
    }
    if extra_kwargs:
        payload["extra_kwargs"] = extra_kwargs

    # Surface any positional args we did not consume so the blob stays
    # round-trippable; skip the leading messages positional when we already
    # promoted it above.
    leftover_args: list[Any] = []
    if messages is not None and args and isinstance(args[0], list):
        leftover_args.extend(args[1:])
    else:
        leftover_args.extend(args)
    if leftover_args:
        payload["args"] = leftover_args

    return payload


def _response_payload(response: Any) -> dict[str, Any]:
    """Build a serializable response payload.

    agentscope 2.0 returns ``ChatResponse.content`` as a list of typed
    blocks (``TextBlock`` / ``ThinkingBlock`` / ``ToolCallBlock`` /
    ``DataBlock``); blocks are pydantic ``BaseModel`` so
    ``sanitize_payload._make_json_safe`` recursively ``model_dump``s
    them downstream.  We keep the 1.x scalar fallback (``text`` /
    ``tool_calls`` / ``finish_reason``) so custom adapters and test
    doubles that still expose the old shape keep working.
    """
    payload: dict[str, Any] = {}

    content = safe_attr(response, "content")
    if isinstance(content, list):
        # Preserve the full ordered block list so downstream consumers
        # can rebuild the round exactly.  ``sanitize_payload`` handles
        # pydantic ``BaseModel`` blocks and truncates per
        # ``max_record_bytes``.
        payload["content"] = list(content)
    else:
        # agentscope 1.x (and a handful of legacy test doubles): scalar
        # fields on the response itself.
        text = safe_attr(response, "text")
        if isinstance(text, str):
            payload["text"] = text
        legacy_tcs = safe_attr(response, "tool_calls")
        if isinstance(legacy_tcs, list):
            payload["tool_calls"] = legacy_tcs

    usage = safe_attr(response, "usage")
    if usage is not None:
        try:
            payload["usage"] = usage.model_dump(mode="json")
        except Exception:
            try:
                payload["usage"] = dict(usage)
            except Exception:
                payload["usage"] = str(usage)

    # 2.0 spells it ``finished_reason``; 1.x used ``finish_reason``.
    finished_reason = safe_attr(response, "finished_reason")
    if finished_reason is None:
        finished_reason = safe_attr(response, "finish_reason")
    if finished_reason is not None:
        payload["finished_reason"] = str(finished_reason)

    return payload


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Normalize tool calls from blocks (2.0) or legacy list (1.x).

    The 2.0 ``ToolCallBlock`` stores the raw JSON argument string on
    ``input``; the 1.x shape exposed ``response.tool_calls`` directly.
    Both are normalized here so the ``TOOL_CALL_REQUEST`` event stays
    consumer-friendly (OpenAI-compatible ``function.arguments`` string).
    """
    content = safe_attr(response, "content")
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for block in content:
            block_type = safe_attr(block, "type")
            if isinstance(block_type, str) and block_type == "tool_call":
                tc_id = safe_attr(block, "id")
                tc_name = safe_attr(block, "name")
                tc_input = safe_attr(block, "input")
                if isinstance(tc_input, str):
                    arguments: str | None = tc_input
                elif isinstance(tc_input, dict):
                    arguments = json.dumps(tc_input, ensure_ascii=False)
                else:
                    arguments = None
                out.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": arguments,
                    },
                })
        if out:
            return out
    legacy = safe_attr(response, "tool_calls")
    if isinstance(legacy, list):
        return list(legacy)
    return []


def _extract_thinking(response: Any) -> str | None:
    """Extract reasoning/thinking content from a response object.

    agentscope 2.0 carries thinking in ``ThinkingBlock.thinking``
    inside ``response.content``; older shapes exposed
    ``response.reasoning_content`` or nested ``extra_content``.  Both
    paths must work — the ``THINKING`` event is the typed shortcut, the
    full ``ThinkingBlock`` is also preserved in ``_response_payload``
    under ``content`` so the reasoning chain survives even if a consumer
    reads only ``MODEL_RESPONSE``.
    """
    content = safe_attr(response, "content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            block_type = safe_attr(block, "type")
            if isinstance(block_type, str) and block_type == "thinking":
                t = safe_attr(block, "thinking")
                if isinstance(t, str):
                    parts.append(t)
        if parts:
            return "".join(parts)
    # 1.x fallback (and the rare adapter that still puts reasoning in
    # ``extra_content``).
    reasoning = safe_attr(response, "reasoning_content")
    if reasoning:
        return str(reasoning)
    extra = safe_attr(response, "extra_content")
    if isinstance(extra, dict):
        reasoning = extra.get("reasoning_content") or extra.get("thinking")
        if reasoning:
            return str(reasoning)
    return None


def _trajectory_context() -> dict[str, Any]:
    """Collect context vars for a trajectory event."""
    try:
        from ..app.agent_context import (
            get_current_agent_id,
            get_current_channel,
            get_current_session_id,
            get_current_trace_id,
            get_current_user_id,
        )

        return {
            "trace_id": get_current_trace_id() or "",
            "session_id": get_current_session_id() or "",
            "agent_id": get_current_agent_id() or "",
            "user_id": get_current_user_id() or "",
            "channel": get_current_channel() or "",
        }
    except Exception:
        return {
            "trace_id": "",
            "session_id": "",
            "agent_id": "",
            "user_id": "",
            "channel": "",
        }

# AgentScope does not expose provider cache semantics through a public
# capability API. These prefixes therefore depend on its concrete adapter MRO
# module paths. Unknown or renamed paths intentionally fail closed so cache
# metrics disappear instead of being reported with an invalid denominator.
_CACHE_USAGE_MODEL_MODULES = (
    "agentscope.model._anthropic",
    "agentscope.model._dashscope",
    "agentscope.model._deepseek",
    "agentscope.model._gemini",
    "agentscope.model._moonshot",
    "agentscope.model._openai_chat",
    "agentscope.model._openai_response",
    "agentscope.model._xai",
)


def _cache_usage_metrics(
    model: Any,
    prompt_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> tuple[bool, int]:
    """Return whether cache usage is supported and its input denominator."""
    modules = {
        cls.__module__
        for cls in type(model).__mro__
        if isinstance(getattr(cls, "__module__", None), str)
    }
    observed = any(
        module.startswith(prefix)
        for module in modules
        for prefix in _CACHE_USAGE_MODEL_MODULES
    )
    if not observed:
        return False, 0
    if any(
        module.startswith("agentscope.model._anthropic") for module in modules
    ):
        return (
            True,
            prompt_tokens + cache_read_tokens + cache_write_tokens,
        )
    if cache_read_tokens + cache_write_tokens > prompt_tokens:
        return False, 0
    return True, prompt_tokens


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    _usage_by_session: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        provider_id: str,
        model: ChatModelBase,
        compact_threshold: float | None = None,
    ) -> None:
        # agentscope 2.0 ChatModelBase requires credential/model/parameters.
        # Forward the wrapped model's own values so the base attributes stay
        # consistent (some downstream code reads ``self.model`` for logging).
        super().__init__(
            credential=getattr(model, "credential", None),
            model=getattr(model, "model", "unknown"),
            parameters=getattr(model, "parameters", None)
            or ChatModelBase.Parameters(),
            stream=getattr(model, "stream", True),
            context_size=getattr(model, "context_size", 32768),
        )
        self._model = model
        # AgentScope 2.0.6 consults ``agent.model.formatter`` before the
        # model call to validate incoming media blocks.  ChatModelBase does
        # not define that attribute itself, so transparent wrappers must
        # preserve the concrete provider model's formatter explicitly.
        formatter = getattr(model, "formatter", None)
        if formatter is not None:
            self.formatter = formatter
        self._provider_id = provider_id
        # Auto-compaction threshold (fraction of the window) for the UI, or
        # None when compaction is disabled/unknown.
        self._compact_threshold = compact_threshold

    @property
    def formatter(self) -> Any:
        """Expose the wrapped model's formatter to AgentScope."""
        return self._model.formatter

    @formatter.setter
    def formatter(self, value: Any) -> None:
        """Keep formatter updates synchronized with the wrapped model."""
        self._model.formatter = value

    def _record_usage(self, usage: ChatUsage | None) -> None:
        """Enqueue a usage event synchronously — never blocks the caller."""
        if usage is None:
            return
        pt = max(int(getattr(usage, "input_tokens", 0) or 0), 0)
        ct = max(int(getattr(usage, "output_tokens", 0) or 0), 0)
        cache_read = max(
            int(getattr(usage, "cache_input_tokens", 0) or 0),
            0,
        )
        cache_write = max(
            int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            0,
        )
        if pt <= 0 and ct <= 0:
            return
        cache_observed, cache_eligible = _cache_usage_metrics(
            self._model,
            pt,
            cache_read,
            cache_write,
        )
        if not cache_observed:
            cache_read = 0
            cache_write = 0

        event = _UsageEvent(
            provider_id=self._provider_id,
            model_name=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            date_str=date.today().isoformat(),
            now_iso=datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds",
            ),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_eligible_input_tokens=cache_eligible,
            cache_observed=cache_observed,
            agent_id=_usage_agent_id(),
        )
        # Fire-and-forget: synchronous put_nowait, ~100 ns, no await needed.
        get_token_usage_manager().enqueue(event)

        usage_data = {
            "provider_id": self._provider_id,
            "model_name": self.model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_eligible_input_tokens": cache_eligible,
            "cache_observed": cache_observed,
            "cache_hit_rate": (
                cache_read / cache_eligible * 100
                if cache_eligible > 0
                else None
            ),
            # Context window of the wrapped model, so the UI can show how full
            # the *current* context is (prompt_tokens / context_size), distinct
            # from the cumulative session totals. 0 = unknown.
            "context_size": int(getattr(self._model, "context_size", 0) or 0),
            # Auto-compaction threshold (fraction of the window) so the UI can
            # mark where context gets evicted. None = disabled/unknown.
            "compact_threshold": self._compact_threshold,
        }
        self._store_usage(usage_data)

    @classmethod
    def pop_usage_for_session(cls, session_id: str) -> dict[str, Any] | None:
        return cls._usage_by_session.pop(session_id, None)

    def _store_usage(self, usage: dict[str, Any] | None) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        if session_id and usage:
            previous = TokenRecordingModelWrapper._usage_by_session.get(
                session_id,
            )
            if previous is None:
                TokenRecordingModelWrapper._usage_by_session[
                    session_id
                ] = usage
                return
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_eligible_input_tokens",
            ):
                usage[key] = int(previous.get(key, 0) or 0) + int(
                    usage.get(key, 0) or 0,
                )
            usage["total_tokens"] = (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
            usage["cache_observed"] = bool(
                previous.get("cache_observed", False)
                or usage.get("cache_observed", False),
            )
            cache_eligible = usage["cache_eligible_input_tokens"]
            usage["cache_hit_rate"] = (
                usage["cache_read_tokens"] / cache_eligible * 100
                if cache_eligible > 0
                else None
            )
            TokenRecordingModelWrapper._usage_by_session[session_id] = usage

    async def generate_structured_output(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        recorder = _get_trajectory_recorder()
        request_span_id: str | None = None
        started_at = time.monotonic()
        if recorder is not None:
            from ..trajectory.models import TrajectoryEventType

            ctx = _trajectory_context()
            event = recorder.record(
                trace_id=ctx["trace_id"],
                event_type=TrajectoryEventType.MODEL_REQUEST,
                payload=_structured_output_payload(args, kwargs),
                session_id=ctx["session_id"],
                agent_id=ctx["agent_id"],
                user_id=ctx["user_id"],
                channel=ctx["channel"],
                provider_id=self._provider_id,
                model_name=self.model,
            )
            request_span_id = event.span_id if event is not None else None

        result = await self._model.generate_structured_output(*args, **kwargs)

        duration_ms = int((time.monotonic() - started_at) * 1000)
        self._record_usage(safe_attr(result, "usage"))
        if recorder is not None:
            self._record_model_response(
                recorder,
                result,
                request_span_id,
                duration_ms,
            )
        return result

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # agentscope 2.0 routes structured output through
        # ``generate_structured_output`` instead of a ``__call__`` kwarg, and
        # provider SDKs (anthropic, openai) reject unknown kwargs. Drop the
        # 1.x ``structured_model`` if a caller still passes it.
        kwargs.pop("structured_model", None)

        # Fix: Omit tool_choice="auto" for vLLM compatibility
        # vLLM without --enable-auto-tool-choice will reject requests when
        # tool_choice="auto" is present, even if tools are provided.
        # By omitting tool_choice when it's "auto", we bypass the check
        # while keeping tools available for correct tool calling behavior.
        if tool_choice == "auto":
            tool_choice = None

        recorder = _get_trajectory_recorder()
        request_span_id: str | None = None
        started_at = time.monotonic()
        if recorder is not None:
            from ..trajectory.models import TrajectoryEventType

            ctx = _trajectory_context()
            event = recorder.record(
                trace_id=ctx["trace_id"],
                event_type=TrajectoryEventType.MODEL_REQUEST,
                payload=_request_payload(messages, tools, tool_choice, kwargs),
                session_id=ctx["session_id"],
                agent_id=ctx["agent_id"],
                user_id=ctx["user_id"],
                channel=ctx["channel"],
                provider_id=self._provider_id,
                model_name=self.model,
            )
            request_span_id = event.span_id if event is not None else None

        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

        duration_ms = int((time.monotonic() - started_at) * 1000)

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result, request_span_id, duration_ms)

        self._record_usage(safe_attr(result, "usage"))
        if recorder is not None:
            self._record_model_response(
                recorder,
                result,
                request_span_id,
                duration_ms,
            )
        return result

    def _record_model_response(
        self,
        recorder: Any,
        response: Any,
        parent_span_id: str | None,
        duration_ms: int,
    ) -> None:
        """Record model_response, tool_call_request and optional thinking event."""
        from ..trajectory.models import TrajectoryEventType

        ctx = _trajectory_context()
        response_event = recorder.record(
            trace_id=ctx["trace_id"],
            event_type=TrajectoryEventType.MODEL_RESPONSE,
            parent_span_id=parent_span_id,
            payload=_response_payload(response),
            metadata={"duration_ms": duration_ms},
            session_id=ctx["session_id"],
            agent_id=ctx["agent_id"],
            user_id=ctx["user_id"],
            channel=ctx["channel"],
            provider_id=self._provider_id,
            model_name=self.model,
        )
        tool_calls = _extract_tool_calls(response)
        if tool_calls:
            recorder.record(
                trace_id=ctx["trace_id"],
                event_type=TrajectoryEventType.TOOL_CALL_REQUEST,
                parent_span_id=response_event.span_id if response_event else None,
                payload={"tool_calls": tool_calls},
                session_id=ctx["session_id"],
                agent_id=ctx["agent_id"],
                user_id=ctx["user_id"],
                channel=ctx["channel"],
                provider_id=self._provider_id,
                model_name=self.model,
            )
        thinking = _extract_thinking(response)
        if thinking:
            recorder.record(
                trace_id=ctx["trace_id"],
                event_type=TrajectoryEventType.THINKING,
                parent_span_id=response_event.span_id if response_event else None,
                payload={"thinking": thinking},
                session_id=ctx["session_id"],
                agent_id=ctx["agent_id"],
                user_id=ctx["user_id"],
                channel=ctx["channel"],
                provider_id=self._provider_id,
                model_name=self.model,
            )

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        request_span_id: str | None = None,
        request_duration_ms: int = 0,
    ) -> AsyncGenerator[ChatResponse, None]:
        last_usage: ChatUsage | None = None
        last_chunk: Any = None
        recorder = _get_trajectory_recorder()
        try:
            async for chunk in stream:
                usage = safe_attr(chunk, "usage")
                if usage is not None:
                    last_usage = usage
                # agentscope 2.0 ``ChatModelBase.__call__`` wraps every
                # provider's stream with ``_StreamAccumulator``: the stream
                # always ends with a final ``is_last=True`` chunk that
                # already carries the fully accumulated content (text +
                # thinking + tool calls + finished_reason). Track it so we
                # record one complete ``MODEL_RESPONSE`` per round instead
                # of stitching partial chunks ourselves.
                last_chunk = chunk
                yield chunk
        finally:
            await stream.aclose()
            self._record_usage(last_usage)
            if recorder is not None and last_chunk is not None:
                self._record_model_response(
                    recorder,
                    last_chunk,
                    request_span_id,
                    request_duration_ms,
                )
