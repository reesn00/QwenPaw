# Trajectory 模块设计文档

> 适用版本：`qwenpaw 2.2.x` 及之后  
> 范围：[src/qwenpaw/trajectory/](src/qwenpaw/trajectory/) 及其在 Runtime / Envelope / ToolCoordinator / TokenRecordingModelWrapper 中的接入点

## 1. 背景与目标

QwenPaw 的 agent 在一次 turn 内会触发模型请求、工具调用、流式响应等多个步骤。出于以下诉求，需要把整个 turn 的事件流持久化下来供后续排错、回放、训练：

- **可观测性**：trace 粒度的入参/出参/工具调用，便于事后回放。
- **隐私合规**：不能把 API Key、Token、Bearer 凭据、base64 媒体原样落盘。
- **容量可控**：长会话可能产生数十 MB 文本，需要大小上限与自动回收。
- **会话隔离**：同一个 agent 在不同会话上产生的轨迹必须互不干扰，可以单独查看、单独删除。

## 2. 关键设计决策

| 决策 | 选择 | 原因 |
|---|---|---|
| 落盘格式 | **JSONL 追加写**（一行一个事件） | append-only，崩溃安全；可直接 `jq`/读行解析 |
| 会话隔离粒度 | **按 `session_id` 拆文件**，每会话一个 `.jsonl` | 历史文件需要按会话排查，不能把所有会话混在同一文件里 |
| 写入路径 | **异步单写**（一个 queue + 一个 consumer + per-session bucket） | 调用方零阻塞；按会话独立 batch 与 flush；锁粒度小 |
| 落盘目录 | `<workspace>/<TRAJECTORY_DIR>/<safe_session_id>.jsonl` | 默认 `trajectory/`；不再生成旧的 `<workspace>/trajectory.jsonl` 单文件 |
| 默认开关 | **默认开启**（`TRAJECTORY_ENABLED=True`） | 历史成本可控；通过环境变量或 `agent_config.running.trajectory_config.enabled` 关闭 |
| 隐私脱敏 | payload **sanitize**（正则 + 键名匹配）后再 truncate | 先清洗、再裁剪，避免敏感数据被一并截断保留 |
| 失败回压 | queue 满 → 丢弃并打 `WARNING` 日志 | 落盘是旁路，**不能**阻塞主业务；事件比主路径低优先 |
| 失败重试 | flush 失败 → 写回 bucket 末尾，由下一次 tick 重试 | 偶发磁盘错误不会丢事件 |

## 3. 目录与文件布局

```
<workspace>/
└── trajectory/                       # TRAJECTORY_DIR, 默认 "trajectory"
    ├── <session_id_A>.jsonl          # 每个会话一个 JSONL
    ├── <session_id_B>.jsonl
    └── ...
```

文件名清洗见 [`_safe_session_filename`](src/qwenpaw/trajectory/buffer.py#L39)：

- 仅允许 `[A-Za-z0-9._-]`，其余字符替换为单 `_`。
- 长度上限 `_MAX_SESSION_FILENAME_LEN = 200`。
- 空串 / `.` / `..` / sanitize 后为空 → 退化为 `_default`（事件级 `session_id=""` 全部落到 `_default.jsonl`）。
- 不允许 `abc/../def` 这种穿越：路径分隔符 `/` 会被替换为 `_`。

## 4. 模块构成

| 文件 | 职责 |
|---|---|
| [`models.py`](src/qwenpaw/trajectory/models.py) | `TrajectoryEventType` 枚举、`TrajectoryConfig` 配置模型、`TrajectoryEvent` 事件模型、`sanitize_payload` payload 脱敏与截断 |
| [`recorder.py`](src/qwenpaw/trajectory/recorder.py) | `TrajectoryRecorder`：高层封装，做 sanitize + 构造事件 + `buffer.enqueue` |
| [`buffer.py`](src/qwenpaw/trajectory/buffer.py) | `TrajectoryBuffer` + `_SessionBucket`：异步队列、按 session_id 路由、批量 flush、轮转 |
| [`storage.py`](src/qwenpaw/trajectory/storage.py) | 同步落盘 `save_data_sync`、轮转 `rotate_jsonl`（按 mtime / 总量字节） |
| [`service.py`](src/qwenpaw/trajectory/service.py) | `TrajectoryService`（per-workspace）+ 全局 `_registry[agent_id]` + `build_trajectory_config()` |
| [`__init__.py`](src/qwenpaw/trajectory/__init__.py) | 对外 re-export：`TrajectoryService`、`TrajectoryRecorder`、`TrajectoryConfig`、`TrajectoryEvent`、`TrajectoryEventType`、`get_trajectory_service` / `register_trajectory_service` / `unregister_trajectory_service` |

## 5. 数据模型

### 5.1 事件类型 `TrajectoryEventType`

```python
TURN_START       = "turn_start"        # turn 入口，标注 provider/model/agent_backend
MODEL_REQUEST    = "model_request"     # 发往 LLM 的请求（messages/tools/schema 等）
MODEL_RESPONSE   = "model_response"    # LLM 响应（含 usage + 完整 content blocks）
TOOL_CALL_REQUEST = "tool_call_request" # 模型要求调工具（tool_calls + arguments）
TOOL_EXECUTION   = "tool_execution"    # 实际执行工具的入参与结果
# NOTE: 历史上曾有 typed THINKING 事件；2026-09 删除 —— 它与
# model_response.payload.content[type=thinking] 内容完全重复，
# 单源足以覆盖回放/训练/排错。旧 .jsonl 中的 "thinking" 字符串保留兼容。
ERROR            = "error"             # 阶段异常
CANCEL           = "cancel"            # 用户取消
FINAL_REPLY      = "final_reply"       # 最终发送给用户的内容
```

### 5.2 事件 `TrajectoryEvent`

```python
trace_id: str                 # 请求级 trace 标识（贯穿整次 turn）
span_id: str                  # 当前事件 span（默认 uuid4）
parent_span_id: Optional[str] # request -> response / tool_call_request -> tool_execution

event_type: TrajectoryEventType
timestamp: str                # ISO-8601 UTC，构造时填充
session_id / agent_id / user_id / channel
provider_id / model_name

payload: dict                 # sanitize 后的事件级 payload
metadata: dict                # 阶段级元数据（例如 usage）
```

### 5.3 配置 `TrajectoryConfig`

```python
enabled: bool = True
max_record_bytes: int = 256 * 1024   # 单事件上限
retention_days: int = 30             # 0 = 不过期
flush_interval_seconds: int = 10
redact_patterns: list[str] = []      # 额外正则
```

优先级（[constant.py:234-264](src/qwenpaw/constant.py#L234) → [service.py:87](src/qwenpaw/trajectory/service.py#L87)）：

```
agent_config.running.trajectory_config  >  QWENPAW_TRAJECTORY_*  >  内置默认
```

具体环境变量：

| 变量 | 默认 | 含义 |
|---|---|---|
| `QWENPAW_TRAJECTORY_ENABLED` | `True` | 全局开关 |
| `QWENPAW_TRAJECTORY_MAX_RECORD_BYTES` | `262144` | 单事件字节上限 |
| `QWENPAW_TRAJECTORY_RETENTION_DAYS` | `30` | 文件保留天数（0=永不过期） |
| `QWENPAW_TRAJECTORY_FLUSH_INTERVAL` | `10` | flush 周期（秒） |
| `QWENPAW_TRAJECTORY_DIR` | `trajectory` | 落盘子目录名 |

## 6. 异步管道

### 6.1 整体流程

```
集成点                          TrajectoryBuffer
┌─────────────┐   enqueue    ┌──────────────────────────────┐
│ recorder    │ ──────────▶  │ asyncio.Queue (10k)          │
│ .record()   │              │       │                       │
└─────────────┘              │       ▼ consumer_loop        │
                             │ _resolve_bucket_key()        │
                             │ _get_or_create_bucket()      │
                             │       │                       │
                             │       ▼                       │
                             │ _SessionBucket.batch[]       │
                             │       │ _BATCH_SIZE=100       │
                             │       │  OR flush_interval    │
                             │       ▼                       │
                             │ save_data_sync() ────▶  disk │
                             │ rotate_jsonl()    ────▶  disk│
                             └──────────────────────────────┘
```

### 6.2 关键不变量

- **单个 consumer 协程**串行调用 `_SessionBucket`，但 **bucket 之间相互独立**（per-bucket `asyncio.Lock`），所以多个会话的 flush 不会互相阻塞。
- **dict 级锁 `_buckets_lock`**（`threading.Lock`）只保护 `_buckets` 这个字典本身的增删，不阻塞消费者主循环。
- **flush 失败**：当前 batch 会重新写回 `bucket.batch` 末尾并标记 `dirty=True`，下一轮 tick / batch 满会再次尝试；不会丢事件。
- **queue 满**：`enqueue` 是 `put_nowait`，满了直接丢弃并 `WARNING` 日志，绝不阻塞调用方。
- **stop 序列**：先停 `_flush_task` 避免新 tick，再 `_queue.join()` 等 consumer 排空，再 cancel consumer，最后 `_flush_once(force=True)` 把残留 batch 全部落盘。

### 6.3 轮转策略

`_rotate_by_age`：删除同目录下 mtime > `retention_days` 的、所有以 `_safe_session_id` 为 stem 前缀的文件。  
`_rotate_by_size`：按 mtime 倒序累加，超过 `max_total_bytes` 的旧文件先删（`max_total_bytes=0` 表示不按大小轮转）。

注意：旋转作用于 **整批** 同 stem 前缀的文件（应对未来的 `<session_id>.1.jsonl` 之类分片），目前每次会话只有一份。

## 7. 隐私 / 大小治理

`sanitize_payload`（[models.py:201](src/qwenpaw/trajectory/models.py#L201)）按顺序处理：

1. `_make_json_safe`：把 `BaseModel`、`Enum`、`bytes`、带 `model_dump()` 的对象递归成可 JSON 化的纯值。
2. `_redact_value`：先按内置正则（`api_key`、`authorization`、`bearer token`、`token`、`secret`、`password`、`x-api-key`）保留前缀只把值换成 `[redacted]`；再递归遍历 dict，按键名命中敏感词时把值换成 `[redacted]`。
3. `_maybe_redact_media`：data / image_url / audio_url / video_url 超过 `max_record_bytes` 时替换为 `<N bytes media omitted>`。
4. 第二轮 `_redact_value`：防御媒体里嵌套的字符串凭据。
5. `_truncate_payload`：最后按字节上限截断单字段，保证整条事件不超 `max_record_bytes`。

> ⚠️ sanitize 是 **尽力而为**。任何新接入的字段若携带敏感数据，需要扩展内置正则或 `redact_patterns`。

## 8. 集成点

| 集成点 | 文件 | 记录的事件 | 关键补充 |
|---|---|---|---|
| Runtime turn 入口 | [`runtime.py:254`](src/qwenpaw/runtime/runtime.py#L254) | `TURN_START` | `_resolve_turn_model` 透过 `TokenRecordingModelWrapper` / `_inner` 找到 `provider_id` 与 `model`；`_resolve_turn_backend` 从 `workspace.config.backend` 取 |
| Runtime 错误 | [`runtime.py:381`](src/qwenpaw/runtime/runtime.py#L381) | `ERROR` | 阶段级异常 |
| Runtime 取消 | [`runtime.py:421`](src/qwenpaw/runtime/runtime.py#L421) | `CANCEL` | 用户主动取消 |
| Envelope 流式结束 | [`envelope.py:46`](src/qwenpaw/runtime/envelope.py#L46) | `FINAL_REPLY` | `metadata.usage` 携带 token 统计 |
| 模型包装器 | [`model_wrapper.py:395`](src/qwenpaw/token_usage/model_wrapper.py#L395)、`:449`、`:499`、`:517` | `MODEL_REQUEST` / `MODEL_RESPONSE` / `TOOL_CALL_REQUEST` / `ERROR` | `_structured_output_payload` 把 kwargs 标准化为 `messages` / `tools` / `tool_choice` / `response_schema` / `extra_kwargs`，避免落盘里出现不透明的 `args`/`kwargs` blob |
| 工具协调器 | [`_coordinator.py:44`](src/qwenpaw/tool_calls/_coordinator.py#L44) | `TOOL_EXECUTION` | `payload.tool_name` / `tool_call_id` / `input` / `output` |

所有集成点都通过 `get_trajectory_service(agent_id)` 拿服务，`None` 时直接返回。`agent_id` 来自 `agent_context.set_current_agent_id()` 上下文变量。

各事件写入的内容契约见 §8.5 **事件内容必须具备项**。

## 8.5 事件内容必须具备项

回放、排错、训练下游消费对轨迹的最低要求。任一必备项缺失即视为"不完整"。所有内容在 [sanitize_payload](src/qwenpaw/trajectory/models.py#L201) 阶段经过脱敏 + 截断（`[redacted]` / `...[truncated]`），因此敏感字段会自动保护、单事件体量被 `max_record_bytes` 兜底。

| 维度 | 必须具备 | 写入方 | 缺失后果 |
|---|---|---|---|
| **完整推理链（CoT 核心）** | 仅 `model_response.payload.content` 中保留 `ThinkingBlock`（2.0 blocks 结构）；1.x 走 `reasoning_content` / `extra_content` fallback，同一事件内承载 | [_response_payload](src/qwenpaw/token_usage/model_wrapper.py#L108)（content 块直接列表化） | 推理发散 / 死循环无法定位；只能从 history 间接 join 重建 |
| **工具定义** | `model_request.payload.tools`（含 messages / tools / tool_choice / response_schema） | [model_wrapper._structured_output_payload](src/qwenpaw/token_usage/model_wrapper.py#L64) / [_request_payload](src/qwenpaw/token_usage/model_wrapper.py#L31) | 看不到模型当时可见的工具集；不同轮工具集变更不可追溯 |
| **工具调用闭环** | `tool_call_request`（parent = `model_response`）→ `tool_execution`（按 `tool_call_id` 关联）→ `final_reply` 中对应的 `plugin_call_output` 块 | [model_wrapper](src/qwenpaw/token_usage/model_wrapper.py#L395) + [_coordinator](src/qwenpaw/tool_calls/_coordinator.py#L30) + [envelope](src/qwenpaw/runtime/envelope.py#L29) | 无法验证"模型要调 → 真调了 → 结果回到对话"的一致性 |
| **完整任务终态** | `final_reply.metadata.status` / `error` / `usage` | [envelope._finalize_response](src/qwenpaw/runtime/envelope.py#L914) | 无法区分 `completed` / `failed` / `cancelled`；终态失败原因不可定位 |
| **model_response 完整内容** | `payload = {content: [...blocks], usage, finished_reason}`（2.0 blocks；1.x fallback 到 `text` / `tool_calls` / `finish_reason`）；流式由 `ChatModelBase.__call__` 基类保证最后一个 `is_last=True` chunk 为完整累积结果（见 [`_StreamAccumulator.build`](https://github.com/agentscope-ai/agentscope/blob/main/src/agentscope/model/_utils.py)） | [model_wrapper._response_payload](src/qwenpaw/token_usage/model_wrapper.py#L120) | 最后一轮无"下一轮"间接记录，**单点依赖 final_reply 归档成功**（历史上 archiver terminal-wait bug 曾打在这） |
| **tool_execution.input 已解析** | `payload.input` 为 dict（从 `ToolCallBlock.input` 的 JSON 字符串解析；解析失败或非 dict → 显式 `None`） | [tool_calls._parse_tool_input](src/qwenpaw/tool_calls/_coordinator.py#L1117) | 事件不自包含；每次分析都要跨事件 join 工具调用侧 |
| **call / result 顺序一致** | 同 session 内所有事件 `timestamp` 严格单调；同一 turn 内不重复 | `TrajectoryEvent.timestamp` 默认 ISO-8601 UTC（构造时填充）；buffer `enqueue` 不重排 | 跨事件 join 失去因果；排查竞态 / 乱序不可行 |

测试侧：`tests/unit/trajectory/test_model_wrapper.py` 的 `FakeChatModel` 改用真实 `ChatResponse` + blocks 验证新路径；`test_tool_hooks.py` 增加 `input=json.dumps(...)` 字符串用例与解析失败用例。

## 9. 全局注册表

```python
_registry: dict[str, TrajectoryService] = {}   # service.py
```

- `register_trajectory_service(agent_id, service)`：工作区启动时调用，写入字典。
- `unregister_trajectory_service(agent_id)`：关闭时移除。
- `get_trajectory_service(agent_id)`：所有集成点读取；未注册返回 `None`。

`TrajectoryRecorder.enabled=False` 时，**consumer / flush 任务不会启动**，注册表里仍可拿到 service 但所有 `record()` 调用立即返回 `None`。

## 10. 测试覆盖

| 测试 | 文件 | 覆盖点 |
|---|---|---|
| `test_buffer.py` | [tests/unit/trajectory/test_buffer.py](tests/unit/trajectory/test_buffer.py) | 异步落盘、按 session 路由、空 session → `_default`、路径穿越防御、文件名清洗、长度截断 |
| `test_storage.py` | [tests/unit/trajectory/test_storage.py](tests/unit/trajectory/test_storage.py) | `append_jsonl` / `rotate_jsonl` 边界 |
| `test_models.py` | [tests/unit/trajectory/test_models.py](tests/unit/trajectory/test_models.py) | sanitize / truncate / redact 行为 |
| `test_service.py` | [tests/unit/trajectory/test_service.py](tests/unit/trajectory/test_service.py) | service 生命周期、配置构建、注册表 |
| `test_runtime_turn_start.py` | [tests/unit/trajectory/test_runtime_turn_start.py](tests/unit/trajectory/test_runtime_turn_start.py) | `_resolve_turn_model` 多层包装、provider/model/backend 落盘 |
| `test_envelope.py` | [tests/unit/trajectory/test_envelope.py](tests/unit/trajectory/test_envelope.py) | `FINAL_REPLY` + usage 落盘 |
| `test_tool_hooks.py` | [tests/unit/trajectory/test_tool_hooks.py](tests/unit/trajectory/test_tool_hooks.py) | `TOOL_EXECUTION` 落盘 + input arguments 落盘 |
| `test_model_wrapper.py` | [tests/unit/trajectory/test_model_wrapper.py](tests/unit/trajectory/test_model_wrapper.py) | `MODEL_REQUEST` / `MODEL_RESPONSE` / `TOOL_CALL_REQUEST` 落盘，`generate_structured_output` 路径 |

## 11. 迁移说明（legacy 单文件）

旧版会生成 `<workspace>/trajectory.jsonl`（来自 `TRAJECTORY_FILE`，已废弃保留常量）。本次重写后：

- **不再生成**单文件 JSONL；只产出 `<workspace>/trajectory/<session_id>.jsonl`。
- 旧 `<workspace>/trajectory.jsonl` **不会被自动迁移**。如果有历史数据需要保留，建议用户在升级前手动重命名 / 拆分；本次升级默认开启，旧文件保留不影响新事件。
- `TRAJECTORY_FILE` 常量保留以保持向后 import，但不再被任何代码引用。

## 12. 运维与排错

| 现象 | 排查点 |
|---|---|
| `WARNING: trajectory: queue full, dropping event ...` | 落盘比生成慢（disk 慢、磁盘满、flush 阻塞）。先查 `df`/`iostat`，必要时调大 `QWENPAW_TRAJECTORY_FLUSH_INTERVAL` 或临时 `QWENPAW_TRAJECTORY_ENABLED=false` |
| 单事件超过 `max_record_bytes` | sanitize 截断会标记 `...[truncated]`。如果出现频率高，检查上游是否在传大段 base64 媒体 / 长 system prompt |
| 找不到事件文件 | 检查 `<workspace>/trajectory/` 是否被禁用（`QWENPAW_TRAJECTORY_ENABLED=false` 或 `agent_config.running.trajectory_config.enabled=False`） |
| 同会话多个文件 | 检查是否有 `session_id` 大小写不一致、或 session 边界清理；bucket key 直接用 `session_id` 字符串 |
| `OSError: failed to append to ...` | `_flush_bucket_locked` 会把 batch 写回并 `dirty=True` 等下次重试；查 `ls -la <workspace>/trajectory/` 权限 |

## 13. 已知约束与后续 TODO

- **跨进程共享**：当前 `_registry` 是单进程字典，多 worker 部署需要各自注册各自的 `TrajectoryService`（目前没有跨进程聚合需求）。
- **回放工具**：JSONL 已结构化，但暂未提供官方回放/查询 CLI。后续可以加 `qwenpaw trajectory inspect <session>` 子命令。
- **写入合并**：当前每会话一个 batch，活跃会话数大时 disk IO 仍较多；后续可考虑合并相邻小文件（`_rotate_by_size` 已经准备好接口）。
- **指标暴露**：当前只有日志。后续可把 queue 长度、drop 次数、flush 延迟接入 metrics。