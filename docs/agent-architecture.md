# QwenPaw 后端 Agent 系统架构

> 本文档描述 QwenPaw 后端 Agent 系统的核心架构、主要模块和请求全链路控制流。
> 适合初次接触 Agent 系统的开发者快速建立全局视图。

---

## 1. 请求全链路控制流

用户的一次消息从接收 → 处理 → 回复的完整路径：

```
Channel (钉钉/Discord/微信/...)                  ← app/channels/
  ↓  (将 native payload 统一转换为 content_parts)
FastAPI Router                                   ← app/routers/
  ↓
DynamicMultiAgentRunner._get_workspace(request)  ← app/_app.py
  ↓  根据 agent_id 查找对应 Workspace
WorkspaceRegistry                                ← app/workspace_registry.py
  ↓
Runtime.run()  ← 8 阶段生命周期                  ← runtime/runtime.py
  │
  ├─ [Phase 1] PRE_DISPATCH        (hooks, 含 BootstrapHook)
  ├─ ─── 固定: Slash Command 派发   (如 /help /skill → 命中则 SHORT_CIRCUIT)
  ├─ [Phase 2] POST_DISPATCH        (hooks)
  ├─ [Phase 3] PRE_AGENT_BUILD      (hooks, 含 coding 模式的 ProjectDirInjectionHook)
  ├─ ─── 固定: AgentBuilder.build()  ← 组装 Agent 的唯一入口 (runtime/builder.py)
  ├─ [Phase 4] POST_AGENT_BUILD     (hooks)
  ├─ [Phase 5] PRE_EXECUTE          (hooks, Bootstrap/env stack)
  ├─ ─── 固定: AgentExecutor.run()   ← 心跳包裹的 agent.reply_stream() (runtime/executor.py)
  │        └─ 每次循环:
  │             QwenPawAgent._reasoning()
  │               ├─ check_pending_gates()       ← 消费上轮延迟的 STOP/CONTINUE
  │               ├─ 主动媒体剥离 (proactive media stripping)
  │               ├─ model call (带 media 错误的被动重试)
  │               ├─ _run_stop_handlers → run_stop_handlers → filter_by_scope → Gates
  │               └─ 工具调用迭代 → apply_stop_result (设置 pending，延迟到下一轮)
  ├─ [Phase 6] POST_RESPONSE        (hooks, 会话保存/cron 回写)
  ├─ envelope.finalize()           ← SSE 信封状态机 (runtime/envelope.py)
  ├─ [Phase 7] / [Phase 8] ON_ERROR / FINALLY
  └─ agent.close()   (governor.stop, scroll.purge, offloader.cleanup)
```

**架构不变量**：`AgentBuilder.build()` 和 `AgentExecutor.run()` 是**唯一**触碰 agent 的固定代码。其他一切（AOP 扩展 / 行为注入）都走 hooks 系统。

---

## 2. 核心模块职责一览

| 模块 | 路径 | 职责 |
|------|------|------|
| FastAPI 应用 | `app/_app.py`, `app/routers/` | uvicorn 后端、REST 端点、生命周期、跨 workspace 服务 |
| Agent 主类 | `agents/react_agent.py` | `QwenPawAgent` CodingModeMixin + agentscope.Agent |
| Agent 构造器 | `runtime/builder.py` | `AgentBuilder.build()` — 组装 agent 的唯一入口 |
| 8 阶段运行时 | `runtime/runtime.py`, `runtime/phases.py` | 每 workspace 一个 Runtime 实例 |
| 请求信封 | `runtime/envelope.py` | SSE 事件 → 业务 schema 的转换状态机 |
| Agent 执行器 | `runtime/executor.py` | 心跳包裹的 reply_stream 驱动器 |
| 工具注册表 | `runtime/tool_registry.py` | `@tool_descriptor` 装饰器 + ToolRegistry + ToolDescriptor |
| 工具权限 | `runtime/tool_guard.py` + `security/tool_guard/` | 调用前 YAML regex 规则引擎 + GuardedFunctionTool |
| 内置工具组 | `agents/tools/` | read_file / write_file / execute_shell_command / browser_use / ast_search 等 |
| 中间件 | `agents/middlewares.py` | MemoryMiddleware / ToolResultPruningMiddleware / LangfuseToolSpanMiddleware |
| 技能系统 | `agents/skill_system/` | SkillPoolService / SkillService / manifest / 内置 vs 用户合并 |
| 技能目录 | `agents/skills/` | 内置技能（SKILL.md + references/ + scripts/） |
| 工作区 | `app/workspace/`, `app/workspace_registry.py` | Workspace（每 agent 一个）/ WorkspacePlugins / WorkspaceRegistry |
| 门禁系统 (Loop Engineering) | `loop/gates/`, `loop/react_gates.py`, `loop/handler_registry.py` | StopHandler + StopAction + Gate 体系 (IterationGate / DoomLoopGate / BudgetGate / RubricGate / FileLoopGate) + scope 隔离 + 延迟执行 |
| Agent 模式 | `modes/base.py`, `modes/coding/`, `modes/goal/`, `modes/mission/` | 模式协议（4 个内容方法 + is_active） |
| 模型工厂 | `agents/model_factory.py` + `routing_chat_model.py` | 视频/file 块转换 + TokenRecordingModelWrapper + RoutingChatModel |
| 配置 | `config/` | AgentProfileConfig / AgentsRunningConfig / ContextVars |
| 内存 | `agents/memory/` | BaseMemoryManager + ADBPG / ReMeLight / Noop 后端 + 主动式记忆 |
| 策略 | `security/tool_guard/`, `security/skill_scanner/`, `governance/`, `sandbox/` | 工具门禁 / 技能扫描 / 治理审计 / 沙箱隔离 |
| Lifecycle Hooks | `hooks/`, `hooks/bootstrap/`, `runtime/hooks.py` | Bootstrap/cron/error/observability/request-setup/session/skill-env |
| CLI | `cli/`, `cli/tui/` | copaw/qwenpaw 命令行 + Textual TUI |
| Provider | `providers/`, `providers/provider_manager.py` | LLM 后端（OpenAI/Anthropic/DashScope/Ollama/Gemini/...） |
| Channels | `app/channels/`, `app/channels/registry.py` | BaseChannel 子类 + 内建/自定义 channel 注册 |
| 统计 | `agent_stats/`, `observability/`, `token_usage/` | 用量/调用/token 可观测 |
| 驱动 | `drivers/` | DriverCard/Policy/ACP/MCP 统一抽象层 |
| 备份 | `backup/` | 工作区/配置备份与恢复 |
| 市场 | `market/` | Skills Hub / 社区市场 |

---

## 3. Agent 构建 (AgentBuilder.build)

文件：`src/qwenpaw/runtime/builder.py`

`build(ctx)` 的执行顺序：

1. 加载配置 `load_agent_config(agent_id)`
2. 应用 ACP 编码项目 `_apply_request_coding_project(ctx)`
3. 校验模型可用性 `active_model`
4. 初始化技能 + 解析有效技能 `ensure_skills_initialized(workspace_dir)` + `resolve_effective_skills(workspace_dir, channel)`
5. 计算活跃模式 `workspace.plugins.active_mode_names(ctx)`
6. 初始化资源治理 `ResourceGovernor`
7. 收集额外工具（coding 模式工具 + driver 工具）
8. 构建模型 + formatter：`build_model()` → `model_factory.create_model_and_formatter()` → `TokenRecordingModelWrapper` + `RetryChatModel`
9. 构建 offloader：`QwenPawOffloader`
10. 可选构建 scroll 上下文策略（`strategy="scroll"` 时启用）
11. 构建工具包：`build_toolkit(...)` — 本地 workspace list_tools + 额外工具 + skill 目录
12. 构建系统提示词：`build_prompt()` → `PromptManager`
13. 构建中间件：`_build_middlewares()`
14. 构造 `QwenPawAgent(...)` 注入全部依赖
15. 注册 ReAct 默认 gate：`register_react_gates(workspace, running_config)` — 幂等注册 IterationGate / DoomLoopGate / StandaloneRubricGate（受 `loop.iteration/doom_loop/rubric.enabled` 控制）
16. 加载会话状态：`agent.load_state_dict(ctx.session_state)`

---

## 4. QwenPawAgent 核心类

文件：`src/qwenpaw/agents/react_agent.py`

继承：`QwenPawAgent(CodingModeMixin, agentscope.Agent)`

### 三个核心重写

**`_reasoning(tool_choice)`** — 推理循环的指挥中心（详见上方 "Gate 集成"）：

1. 消费延迟态 (`check_pending_gates`)
2. 主动媒体剥离 + 模型调用（被动重试介质错误）
3. 执行 stop handlers (`_run_stop_handlers`)
4. 按 `final_msg` 分流：工具调用 → 设置延迟态；文本 + CONTINUE → 注入 continuation；否则 yield 让模型停

**`_reply(**kwargs)`** — 注入挂起的后台工具提示

**`compress_context()`** — 委托给 context_manager（scroll 模式）或原生压缩

### 集成的依赖（全部外部注入）

| 依赖 | 来源 |
|------|------|
| model (ChatModel) | `model_factory` + `RoutingChatModel` |
| system_prompt | `PromptManager` |
| toolkit | `ToolRegistry.filter()` |
| react_config | 迭代预算、工具选择 |
| middlewares | 见 §6 |
| memory_manager | 见 §9 |
| governor (ResourceGovernor) | 每 workspace 一个 |
| offloader | `QwenPawOffloader` |
| effective_skills | 见 §8 |

### Gate 集成（Loop Engineering）

QwenPawAgent 内部维护两个延迟状态字段：

- `_gate_pending_stop: StopHandlerResult | None` — 工具调用迭代中的 STOP 暂存
- `_gate_pending_continue: str | None` — 工具调用迭代中的 continue message 暂存

每轮 `_reasoning` 的执行顺序：

1. **入口消费延迟态** — `check_pending_gates(self)` 优先于模型调用：有 pending stop 则输出并 return；有 pending continue 则注入 `LOOP_CONTINUATION_MESSAGE_TAG` 标记的 user Msg
2. **模型调用** — 含主动 media stripping + media-error 被动重试
3. **Stop handler 评估** — `_run_stop_handlers(final_msg)` → `PluginRegistry.get_stop_handlers` → `run_stop_handlers`
4. **分流**：
   - `final_msg is None`（工具调用）→ `apply_stop_result` 设置延迟态，return
   - `final_msg` 有文本 + CONTINUE → 注入 continuation message 作为 user Msg，return（外层继续）
   - `final_msg` 有文本 + TERMINATE/BYPASS → yield final_msg（模型想停就停）

### 关键内部逻辑

- `_register_skills(toolkit, effective_skills)` — 加载 workspace 技能目录到 `toolkit._qp_skills`
- `memory_manager.list_memory_tools()` — 内存工具包装为 `GuardedFunctionTool` 追加到 `toolkit.basic_group`
- 强制 `PermissionMode.BYPASS` 绕过 agentscope 内置权限引擎
- 每工具默认超时通过 `ToolCoordinatorMiddleware` 注册
- `state_dict()` / `load_state_dict()` — AgentState 持久化，含 1.x → 2.x 内存迁移
- `close()` — 关停 governor、清理 scroll 历史、清理 offloader

---

## 5. 系统提示词组装

文件：`src/qwenpaw/runtime/prompt_manager.py`、`prompt_contributors.py`、`agents/prompt_builder.py`

### PromptContributor 模型（8+ 个内置贡献者）

| 名称 | 优先级 | 数据来源 |
|------|--------|----------|
| `AgentIdentityContributor` | 5 | `# Agent Identity` agent_id 块 |
| `AgentsMdContributor` | 10 | `AGENTS.md`（剥离 heartbeat/memory XML 注释，注入 memory prompt） |
| `SoulMdContributor` | 20 | `SOUL.md` |
| `ProfileMdContributor` | 30 | `PROFILE.md` |
| `MultimodalHintContributor` | 80 | `build_multimodal_hint()` |
| `CodingModeContributor` | 85 | `_CODING_SYSTEM_PROMPT_TEMPLATE`（来自 coding/mixin） |
| `ScrollContextContributor` | 86 | `build_scroll_system_prompt(language)` |
| `DriverPolicyHintContributor` | 88 | `ctx.extras["driver_prompt_hints"]` |
| `EnvContextContributor` | 90 | `ctx.extras["env_context"]`（时间/会话/OS 块） |

贡献者按 `priority` 升序拼接，用 `PROMPT_SEPARATOR = "\n\n"` 连接；抛异常时单个贡献者被日志记录并跳过（不中断整个 prompt 构建）。

### 插件注入双系统

- **`PromptBuilder.HOST_ANCHORS = ("workspace", "multimodal", "env_context")`** — 旧锚点模型，插件通过 `PluginRegistry.get_prompt_sections()` 按 anchor + agent_id 过滤注入
- **`PromptManager`** — 贡献者优先级模型（新的）

`AgentBuilder.build_prompt()` **优先使用** `plugins.prompt_manager`（如果有注册的 contributor），否则回退到构建默认 PromptManager。

---

## 6. 中间件系统

文件：`src/qwenpaw/agents/middlewares.py`

通过 agentscope `MiddlewareBase` 钩子包裹 agent 的内层推理循环。

### MemoryMiddleware

拥有生命周期级内存行为：

1. `on_system_prompt` — 追加 `memory_manager.get_memory_prompt()` 到 system prompt
2. `on_model_call` — 调用 `memory_manager.auto_memory_search()` 在用户轮注入相关记忆上下文
3. `on_reply` — 按计划调度 `auto_memory()` 提取（`_auto_memory_interval()`）
4. `on_compress_context` — 在上下文压缩前 flush pending auto-memory（`summarize_when_compact` 启用时）
5. 对自动化请求（`source ∈ {cron, heartbeat}`）跳过 auto-memory
6. `_repair_tagged_auto_memory_search_context()` — 拆分被合并到 auto-search 消息中的真实回复块

### ToolResultPruningMiddleware

分层截断工具输出：

- 最近工具结果 vs 旧工具结果用不同字节上限（`DEFAULT_MAX_BYTES = 50KB`，`MAX_FILE_READ_BYTES = 200MB`）
- 豁免工具（`read_file` 对特定扩展名在 `exempt_file_extensions` 中）总是保留上限
- 截断前全量保存到 `{tool_results_dir}/{uuid}.txt`（`truncate_text_output()`）

### LangfuseToolSpanMiddleware

可观测性包装：在 trace 活跃时记录每个工具执行为一个 Langfuse tool observation。

### ToolCoordinatorMiddleware（runtime 层）

HITL（human-in-the-loop）工具调用协调：

- `on_acting` 拦截需要审批的工具调用 → 挂起 → 等待 `ToolCoordinator` 返回
- 也用于每工具默认超时的 `ToolCoordinatorMiddleware.hooks`

### 构建顺序

`_build_middlewares()` 组装：

```
ToolCoordinatorMiddleware → memory middlewares →
ToolResultPruningMiddleware → LangfuseToolSpanMiddleware →
plugin middlewares
```

---

## 7. 工具系统

### @tool_descriptor 装饰器模型

文件：`src/qwenpaw/runtime/tool_registry.py`

```python
@tool_descriptor(
    name="read_file",
    requires_sandbox=("file_read",),
    async_execution=True,
    enabled_by_default=True,
    requires_modes=(),       # 指定模式之一激活时才可用
    requires_skills=(),      # 需要启用指定技能
    requires_features=(),    # 需要全部功能标志
    requires_sandbox=(),     # sandbox 资源需求声明
)
```

`ToolDescriptor` 是 frozen dataclass；装饰器执行时写入 `_REGISTERED_TOOL_FUNC` 全局表。

### ToolRegistry.filter 决策流程

```
denied 集合命中 → 跳过
非空 allowed 集合且不在其中 → 跳过
（注：enabled_by_default=False 的工具在 allowed 中仍通过）
requires_modes 非空 且 不在 active_modes → 跳过
requires_skills 非空 且 不在 active_skills → 跳过
requires_features 中存在不在 enabled_features 的 → 跳过
→ 通过
```

### 内置工具清单

| 文件 | 工具函数 | sandbox 需求 |
|------|----------|-------------|
| `file_io.py` | `read_file` / `write_file` / `edit_file` / `append_file` | file_read / file_write |
| `file_search.py` | `grep_search` / `glob_search` | file_read |
| `shell.py` | `execute_shell_command` | shell_exec |
| `browser_control.py` | `browser_use` | — |
| `browser_snapshot.py` | snapshot 工具 | — |
| `desktop_screenshot.py` | `desktop_screenshot` | file_write |
| `view_media.py` | `view_image` / `view_video` | file_read |
| `get_current_time.py` | `get_current_time` / `set_user_timezone` | — |
| `get_token_usage.py` | `get_token_usage` | — |
| `ast_tool.py` | `ast_search` | — |
| `lsp_tool.py` | 5 个 LSP 工具（via `make_lsp_tool`，自动检测可用语言服务器） | — |
| `agent_management.py` | `list_agents` / `chat_with_agent` / `submit_to_agent` / `check_agent_task` / `spawn_subagent` | — |
| `delegate_external_agent.py` | `delegate_external_agent` | — |
| `make_skill_tools.py` | `materialize_skill` | — |
| `send_file.py` | `send_file` | file_read |
| `run_tool_batch.py` | `run_tool_batch` | — |

添加新工具的步骤：
1. 装饰函数 `@tool_descriptor(...)`
2. 在 `agents/tools/__init__.py` 触发导入装饰器（仅导入即可）
3. 如果必要，在 `AgentBuilder.build_toolkit` 中映射

### GuardedFunctionTool 权限引擎

文件：`src/qwenpaw/runtime/tool_guard.py` + `src/qwenpaw/security/tool_guard/`

优先级链：

1. `approval_level == "bypass"` → ALLOW
2. 引擎禁用 / exec_level 禁用 → ALLOW
3. Engine denied-list 命中 → DENY
4. STRICT 模式 → 合成 INFO 级 guard result 触发审批卡片
5. AUTO/SMART → `engine.guard(name, input)` 检查 findings
6. `should_auto_deny_result()` → DENY
7. SMART + max_severity INFO/LOW → ALLOW
8. 其他 → ASK（阻塞等待 Future，由 `/approval/{approve,deny}` 端点解析）

**拒绝消息**会被自动追加"不要重试"指令（`_with_no_retry_instruction`）。

---

## 8. 技能体系 (Skill System)

路径：`src/qwenpaw/agents/skill_system/`

### 数据流

```
┌──────────────────┐          ┌──────────────────────┐
│ Built-in Skills  │          │   Skill Pool         │
│ (agents/skills/) │──import──▶  skill_pool/         │
│  多语言变体        │          │  + config.skill_paths │
│  (最后同步)       │          └──────────┬───────────┘
└──────────────────┘                     │ download_to_workspace
                                         ▼
                              ┌──────────────────────┐
                              │  Workspace Skills    │
                              │  <workspace>/skills/ │
                              │  含 enabled/channels │
                              │  /config 状态        │
                              └──────────┬───────────┘
                                         │ resolve_effective_skills()
                                         ▼
                              ┌──────────────────────┐
                              │  Effective Skills    │
                              │  (merged + filtered) │
                              └──────────────────────┘
```

### 三类技能来源

- **`"builtin"`** — 与打包内置版本名称 + `version_text` 匹配，上游可追踪
- **`"customized"`** — 用户编辑过的（内容不同但匹配内置名）
- **用户纯自建** — 无内置对应版本

由 `classify_pool_skill_source()` 决定。

### `resolve_effective_skills(workspace_dir, channel_name)` 决策

按 manifest 顺序遍历 entries，筛选规则：

- `enabled=True`
- `channels ∋ {all, channel_name}`
- 技能目录存在

返回的列表即 `effective_skills`，传递给 `AgentBuilder.build()`。

### 服务分层

- **`SkillPoolService`**（`pool_service.py`）= 共享池层，负责创建/import/save/rename/upload/download/auto-update
- **`SkillService`**（`workspace_service.py`）= 每 workspace CRUD、enable/disable、channel routing、zip import

### Skill 配置环境变量注入

`apply_skill_config_env_overrides()` — 将有效技能的 `metadata.requires.env` 键注入环境变量；完整配置以 `QWENPAW_SKILL_CONFIG_<NAME>` JSON 形式注入。

### 技能冲突处理

`suggest_conflict_name()` — 添加 `-YYYYMMDDHHMMSS` 时间戳后缀。下载到 workspace 时 `_check_download_conflict()` 区分：`builtin_upgrade` / `language_switch` / `conflict` / `unchanged`。

---

## 9. 内存系统

路径：`src/qwenpaw/agents/memory/`

### BaseMemoryManager 抽象

所有内存后端必须实现：

- `start()` / `close()`
- `get_memory_prompt()` → 系统提示词引导片段
- `list_memory_tools()` → 暴露给 agent 的内存工具

可选 override：

- `summarize(messages)` — 摘要生成
- `dream()` — 后台记忆优化
- `auto_memory_search(messages, agent_name, **kwargs)` — 调用前自动搜索
- `auto_memory(all_messages, **kwargs)` — 按计划提取

### 内置后端

| 后端 | 文件 | 注册键 |
|------|------|--------|
| `ReMeLightMemoryManager` | `reme_light_memory_manager.py` | `reme_light` |
| `ADBPGMemoryManager` | `adbpg_memory_manager.py` | `adbpg`（ApsandraDB-PG 向量记忆） |
| `NoopMemoryManager` | `dummy.py` | `none` |
| `AgentMdManager` | `agent_md_manager.py` | 管理 AGENTS.md/SOUL.md/PROFILE.md |

按后端字符串从 `memory_registry` 查找，找不到时回退到第一个注册的后端。

### 内存中间件的三部分行为

**MemoryMiddleware** (`agents/middlewares.py`)：

1. `on_system_prompt` → `get_memory_prompt()` 追加到 system prompt
2. `on_model_call` → `auto_memory_search()` 注入相关记忆
3. `on_reply` → 按计划调度 `auto_memory()` 提取
4. `on_compress_context` → 压缩前 flush pending auto-memory

自动化请求（`source ∈ {cron, heartbeat}`）跳过自动 memory。

### 主动式记忆 (Proactive Memory)

`agents/memory/proactive/`:

- 用独立 proactive agent（带 read_file / execute_shell_command / desktop_screenshot / browser_use tools）
- `build_proactive_memory_context()` — 构建记忆上下文
- `PROACTIVE_TASK_EXTRACTION_PROMPT` + `PROACTIVE_USER_FACING_MESSAGE_PROMPT` 双 prompt 流程
- `proactive_trigger.py` — 触发循环/评估
- `proactive_types.py` — `ProactiveConfig` / `ProactiveQueryResult` / `ProactiveTask`

---

## 10. Loop Engineering（门禁系统 / 循环控制）

> QwenPaw 的 Loop Engineering 让 Agent **持续工作多个回合**，直到任务完成、预算耗尽或用户喊停。核心抽象是一套 **Gate（门控）体系**：每轮 ReAct 迭代结束后，多个 Gate 按优先级表决"继续还是停止"。

路径：`src/qwenpaw/loop/`

### 10.1 核心类型

```python
# loop/gates/base.py
class StopAction(str, Enum):
    BYPASS = "bypass"                        # 无意见，跳过
    INTERRUPT_AND_CONTINUE = "interrupt_and_continue"  # 注入消息后继续循环
    TERMINATE = "terminate"                  # 立即停止循环

@dataclass
class StopHandlerResult:
    action: StopAction                       # BYPASS / INTERRUPT_AND_CONTINUE / TERMINATE
    continuation_message: str = ""           # 注入到下一轮的用户消息
    reason: str = ""                         # 决策原因（日志/展示）
    reset_peers: bool = False                # True 时重置其他 gate 状态（开启新 sub-turn）

@dataclass
class StopHandlerRegistration:
    """注册到 workspace.plugins.stop_handlers 的处理单元。"""
    plugin_id: str
    handler: StopHandler
    priority: int = 100                      # handler 间排序（小 → 先）
    name: str = ""
    scope: str = ""                          # 见 §10.6 Scope 隔离

class StopGate(ABC):
    name: str                                # 唯一标识（用于 unregister）
    priority: int = 100                      # 小 → 先执行
    async def check(ctx) -> StopHandlerResult | None
    def build_continuation() -> str          # INTERRUPT_AND_CONTINUE 时调用
    def reset()                              # 新 turn 清零（有状态 gate override）
```

### 10.2 继承层次

```
StopGate (ABC, loop/gates/base.py)
 └── LoopGate (ABC, loop/gates/loop_gate.py)     ← session 隔离（_per_session dict）
      ├── FileLoopGate (file_loop_gate.py)        ← 文件状态 + 迭代上限（Ralph/Ulwork 等基类）
      ├── IterationGate (iteration.py)            ← 硬性迭代上限
      ├── BudgetGate (budget.py)                  ← Token 预算上限
      ├── DoomLoopGate (doom_loop.py)             ← 滑动窗口重复检测
      ├── GoalTurnGate (modes/goal/gates.py)      ← Goal 模式跨请求 turn 计数
      ├── GoalBudgetGate (modes/goal/gates.py)    ← Goal 模式 token 预算
      └── RubricGate (modes/goal/gates.py)        ← Goal 模式 rubric 评估
```

`StandaloneRubricGate` 直接继承 `StopGate`（无 session 状态），独立控制。

### 10.3 内置 Gate 一览

| Gate | 文件 | 优先级 | Scope | 功能 |
|------|------|--------|-------|------|
| `DoomLoopGate` | `gates/doom_loop.py` | 5 | default | 滑动窗口重复检测（`window_size`, `similarity_threshold`），`stages` 多级升级：`modify_prompt` → `INTERRUPT_AND_CONTINUE`，`stop` → `TERMINATE` |
| `IterationGate` | `gates/iteration.py` | 10 | default | 硬性迭代上限 (`max_iterations`)，每 session 独立计数 |
| `BudgetGate` | `gates/budget.py` | 20 | default | Token 预算上限 (`max_tokens`)，由 `update_tokens()` 写入当前用量 |
| `GoalTurnGate` | `modes/goal/gates.py` | 10 | "" | Goal 模式跨请求 turn 计数（外层循环） |
| `GoalBudgetGate` | `modes/goal/gates.py` | 20 | "" | Goal 模式 token 预算 |
| `RubricGate` | `modes/goal/gates.py` | 30 | "" | Goal 模式 rubric 评估（SATISFIED → TERMINATE） |
| `StandaloneRubricGate` | `gates/rubric.py` | 90 | default | 纯文本 re-prompt，最多干预 `max_interventions` 次 |
| `FileLoopGate` | `gates/file_loop_gate.py` | 90 | 插件自定义 | 文件状态 + 迭代循环基类（`_is_complete(state_dir)` 由子类实现） |
| `MissionGate` | `modes/mission/gates.py` | — | mission | Mission 阶段 2 执行驱动器（独立 StopHandler） |

### 10.4 StopHandler 表决流程

文件：`loop/gates/handler.py`

```
for gate in sorted(gates, key=priority):
    result = await gate.check(ctx)
    if result is None or result.action == BYPASS:
        continue                    # 无意见，下一个
    if result.action == TERMINATE:
        return result               # 立即停止
    # INTERRUPT_AND_CONTINUE
    has_continue = True
    continue_result ||= result      # 首个 CONTINUE 胜出
    continue_gate ||= gate
# 全 BYPASS / 无 gates
return TERMINATE                    # 缺省行为：停止

# 胜出 CONTINUE gate 调用
msg = continue_gate.build_continuation()
# reset_peers=True 时重置其他所有 gate 状态（开启新 sub-turn）
```

**关键决策**：TERMINATE > INTERRUPT_AND_CONTINUE > BYPASS；**全缺省 = TERMINATE**（无 gates 或全 BYPASS → 停止）。

### 10.5 Runner 层：Scope 过滤 + 延迟执行

文件：`loop/gates/runner.py`

**多 handler 执行**（`run_stop_handlers`）：

```
filter_by_scope(handlers)     ← 见 §10.6
sort(handlers, key=priority)
for reg in handlers:
    result = await reg.handler(ctx)   # ctx = {agent, final_msg, iteration, has_tool_calls}
    if result.action ∈ {TERMINATE, INTERRUPT_AND_CONTINUE}:
        return result
return TERMINATE                      # 缺省
```

**延迟执行**（`apply_stop_result` / `check_pending_gates`）：

工具调用迭代中 STOP 不会立即生效——系统把结果暂存到 `agent._gate_pending_stop` / `agent._gate_pending_continue`，等 Agent 拿到工具结果后（下一条 `_reasoning`）再消费：

- `check_pending_gates(agent)` — 本轮 `_reasoning` 入口消费：有 pending stop 则输出停止文本并 return；有 pending continue 则注入 `LOOP_CONTINUATION_MESSAGE_TAG` 标记的 user 消息。
- `apply_stop_result(agent, result, is_tool_call=True)` — 工具调用迭代后设置 pending 状态。

这保证**工具不会被执行到一半打断**。

### 10.6 Scope 隔离

`StopHandlerRegistration.scope` 控制 handler 的激活时机（`_filter_by_scope`）：

| scope | 行为 |
|-------|------|
| `""` (空) | 始终运行，不参与 scope 竞争 |
| `"default"` | 普通对话运行；有非 default 的 handler 持有活跃 gate 时自动跳过 |
| `"mission"` | Mission 模式独占（`MissionGate` 以此隔离） |
| 插件自定义值 | 仅在对应模式活跃时运行 |

**逻辑**：扫描所有 handler，若某非 `"default"` scope 的 handler 有 gate 处于 `active` 状态 → 该 scope 成为 `active_scope`，仅该 scope 和空 scope handler 运行，其余跳过。

这保证普通 Goal/Mission 等模式的 gate 互不干扰，模式结束后默认 gate 自动恢复。

### 10.7 Rubric 策略

文件：`loop/gates/rubric.py`

```python
class RubricVerdict(str, Enum):
    SATISFIED / NEEDS_REVISION / FAILED / GRADER_ERROR / MAX_ITERATIONS

class RubricStrategy(ABC):
    async def evaluate(goal, agent_output, iteration) -> RubricEvaluation
```

| 策略 | 行为 |
|------|------|
| `DefaultRubric` | 始终 SATISFIED（无 rubric 需求时使用） |
| `GoalStatusRubric` | `session.active is False` → SATISFIED（由 `update_goal` 工具翻转） |
| `SubAgentRubric` | 占位（返回 GRADER_ERROR，等待基于文件状态的子 agent 验证实现） |

### 10.8 ReAct 模式默认注册

文件：`loop/react_gates.py`

`register_react_gates(workspace, running_config)` **幂等注册**（带 `_react_gates_registered` 标志；重复调用仅重置 gates）：

1. `IterationGate` — 若 `loop.iteration.enabled`，`activate()` 设上限
2. `DoomLoopGate` — 若 `loop.doom_loop.enabled`，配置 `window_size/stages`
3. `StandaloneRubricGate` — 若 `loop.rubric.enabled`，配置 `prompt/max_interventions`

`resolve_max_iterations(running_config)` 优先级：`loop.iteration.max_iterations > running_config.max_iters`（legacy 兼容）。

在 `AgentBuilder.build()` 末尾调用（`workspace` 非空时），user 每次 turn 前会 `_reset_gates_for_new_turn` 清零各 gate 状态。

### 10.9 Handler 注册表

文件：`loop/handler_registry.py`

`get_or_create_stop_handler(workspace, scope="", priority=0, ...)`：

- 每 workspace 单例（`workspace._stop_handler`）
- 自动包装为 `StopHandlerRegistration` 追加到 `workspace.plugins.stop_handlers`
- Goal 模式 `/ Mission 模式均调用此函数向同一 handler 注册各自的 gate

`PluginRegistry.get_stop_handlers(agent_id)` 汇总 `workspace.plugins.stop_handlers` 返回给 `_run_stop_handlers`。

### 10.10 Gate 配置（agent.json）

对应 `config/config.py` 的 `AgentsRunningConfig.loop`（`LoopConfig`）：

```json
{
  "running": {
    "max_iters": 100,
    "loop": {
      "iteration": { "enabled": true, "max_iterations": null },
      "doom_loop": {
        "enabled": true,
        "window_size": 3,
        "similarity_threshold": 1.0,
        "stages": [
          { "after": 3, "action": "modify_prompt", "prompt": "[WARNING]..." },
          { "after": 6, "action": "stop", "prompt": "Doom loop: ..." }
        ],
        "in_loop_modes": false
      },
      "rubric": {
        "enabled": false,
        "prompt": "You did not call any tool...",
        "max_interventions": 1,
        "in_loop_modes": false
      }
    }
  }
}
```

`in_loop_modes` 字段控制该 gate 是否也在 `/goal` `/mission` 中生效（默认只在普通对话生效，Goal/Mission 有各自的 gate 集）。

### 10.11 Loop Engineering 设计原则

1. **Gate 是可插拔的抽象** — 内置 gate 覆盖迭代/预算/重复/质量四个维度；插件可继承 `LoopGate`/`FileLoopGate` 注册自定义 gate（§16 开发扩展）。
2. **延迟执行** — 工具调用中收到 STOP 不立即打断，等工具结果返回后在下一轮 `_reasoning` 入口消费。
3. **Session 隔离** — `LoopGate` 内部用 `_sessions[session_id]` 字典，多用户/多会话并发独立。
4. **Scope 隔离** — 模式专属 handler 活跃时默认 handler 自动退让。
5. **缺省安全** — 全 BYPASS / 无 gates → TERMINATE（宁可停止也不无限循环）。

---

## 11. Agent 模式

路径：`src/qwenpaw/modes/`

### AgentMode 协议

```python
class AgentMode:
    name: str                          # 唯一标识
    def setup(workspace): ...          # 注册命令/工具/hooks/prompt_contributor
    def commands() -> list: ...
    def tools() -> list: ...
    def hooks() -> list: ...
    def prompt_contributors() -> list: ...
    def is_active(ctx) -> bool: ...    # 子类必须 override，默认 False
```

已激活的模式由 `workspace.plugins.active_mode_names(ctx)` 计算。

### Coding 模式（`modes/coding/`）

- `CodingModeMixin` —混入 QwenPawAgent
- 注入 `_CODING_SYSTEM_PROMPT_TEMPLATE`：
  - 任务跟踪：强制使用 `SLUG_TODO.md` 规划工作流
  - 代码引用格式：`path/to/file.py:42`
  - 工具偏好：`lsp > ast_search > grep_search`
  - File 操作纪律：read-before-edit
- `collect_coding_tools()` 收集 AST / LSP 工具
- `ProjectDirInjectionHook` — `PRE_AGENT_BUILD` priority 30，stashes `project_dir` 到 `ctx.mode_state["coding"]["project_dir"]`

### Goal 模式（`modes/goal/`）

文件：`modes/goal/`

- `GoalMode(AgentMode)` — 注册 `/goal` slash 命令，激活后创建 `GoalSession`（`goal/active/iteration/tokens_used` 等字段）
- 注册 3 个 Goal 专用工具：`get_goal`（查看进度）/ `create_goal`（创建新目标）/ `update_goal`（标记 complete/blocked，翻转 `session.active`）
- 注册 3 个专属 gate（均直接挂到 universal handler）：

  | Gate | 优先级 | 作用 |
  |------|--------|------|
  | `GoalTurnGate` | 10 | 跨请求 turn 计数（外层循环），与 `IterationGate` 的 per-request ReAct 迭代（内层）不同 |
  | `GoalBudgetGate` | 20 | goal token 预算上限 |
  | `RubricGate` | 30 | 委托 `GoalStatusRubric` — `session.active is False` → SATISFIED → TERMINATE |

- `GoalStatusRubric`（`loop/gates/rubric.py`）— 唯一非空 rubric，通过 `get_session_fn` ContextVar 回调获取当前 session
- 提供 `GoalPromptContributor` — 根据 turn 数注入 `INITIAL_GOAL_PROMPT` 或 `CONTINUATION_PROMPT`
- 可选注册外部 `completion_gate` / `doom_loop_gate`（受 `in_loop_modes` 配置控制）

### Mission 模式（`modes/mission/`）

文件：`modes/mission/`

- `MissionMode(AgentMode)` — 注册 `/mission` 命令（含 `status`/`list` 子命令）
- **`scope="mission"` 隔离**：创建独立 `StopHandler` + `MissionGate`，注册为 `StopHandlerRegistration(scope="mission")`；活跃时 default handler 自动跳过
- 两条 hook：`MissionStateLoadHook`（PRE_EXECUTE 加载状态）/ `MissionStateSaveHook`（POST_RESPONSE 持久化）
- 由 `MissionPromptContributor` 注入流水线说明
- 状态：`MissionState` + 基于文件的 loop 目录（`start_mission` 返回 `loop_dir`）

---

## 12. 运行时 / 8 阶段编排

路径：`src/qwenpaw/runtime/`

### Phase 枚举

```python
# runtime/phases.py
Phase(str, Enum):
    PRE_DISPATCH        # 请求规范化；SHORT_CIRCUIT/SKIP_AGENT
    POST_DISPATCH       # 派发后 hooks
    PRE_AGENT_BUILD     # 将会话/编码项目注入
    POST_AGENT_BUILD    # 注入模式上下文
    PRE_EXECUTE         # Bootstrap/env stack 推送
    POST_RESPONSE       # 会话保存/cron 回写
    ON_ERROR            # 异常规范化
    FINALLY             # agent.close() 关闭 governance/scroll/offloader
```

### Hook 生命周期

```python
# runtime/hooks.py
HookAction: CONTINUE / SHORT_CIRCUIT / SKIP_AGENT

HookContext(
    request, session_id, agent_id,
    root_session_id, root_agent_id,
    workspace_dir, workspace, app_services,
    input_msgs, agent_config, session_state,
    agent, error, context_injections, mode_state, extras,
)

HookBase(phase, name, priority=100, before=(), after=())
```

`HookRegistry.run(phase, ctx)` 通过 `_topo_sort()` 解析 `before`/`after` 约束，按 `(priority asc, registration index asc)` 排序；cycle 时抛出 `HookCycleError`。

**关键语义**：

- `SHORT_CIRCUIT` — 立即停止当前 phase，返回 Runtime
- `SKIP_AGENT` — 黏性标志；所有 hook 仍执行但最终 action 带 SKIP_AGENT，Runtime 跳过两个固定 agent 步骤
- hook 抛异常**不被吞**，传播到 `ON_ERROR` 链

### AgentExecutor + Envelope

`AgentExecutor(ctx.agent, envelope).run(msgs)`:

- 迭代 `agent.reply_stream(inputs=msgs)`
- 用 `_iter_with_heartbeat(translated, HEARTBEAT_INTERVAL_SECONDS)` 包裹
- 心跳 tick 时 yield `envelope.heartbeat()`
- 其他事件 yield `envelope.translate_event(event)`

`Envelope.translate_event(event)` 是 agentscope EventType → 业务 schema（`AgentResponse`/`Message`/`TextContent`/`DataContent`/`ToolCall`/`ToolResult`）的转换器；跟踪 `_text_blocks`、`_reasoning_blocks`、`_tool_calls`、`_data_blocks`，用 `_seq_counter` + `_tag_seq()` 生成单调递增的 `sequence_number`。

---

## 13. 模型 / Provider 集成

路径：`src/qwenpaw/agents/model_factory.py`、`routing_chat_model.py`、`providers/`

### BaseProvider 抽象

文件：`providers/provider.py`

```python
Provider(
    id, name, base_url, api_key, chat_model,
    models, extra_models, api_key_prefix,
    is_local, freeze_url, require_api_key, is_custom,
    support_model_discovery, support_connection_check,
    generate_kwargs, custom_headers, auth_mode, supports_oauth,
    is_free_tier, provider_group, provider_variant,
    thinking_param_style, reasoning_effort_options,
    thinking_budget_range, meta,
)

ModelInfo(
    id, name,
    supports_multimodal, supports_image, supports_video,
    probe_source, is_free, max_tokens, max_input_length,
    generate_kwargs, preserve_thinking, thinking_enabled,
    thinking_budget, reasoning_effort, thinking_param_style,
    reasoning_effort_options, thinking_budget_range,
)
```

抽象方法：`check_connection()` / `fetch_models()` / `check_model_connection()` / `get_chat_model_instance()`。

### 模型工厂流程

`model_factory.create_model_and_formatter(agent_id)`:

1. `_supports_multimodal_for_current_model()` 判断
2. `_fixup_media_list(items)` — 剥离 file://、缺失文件→占位符、文件块→文本
3. `_create_file_block_support_formatter(base_formatter_class)` — 视频+文件块补丁
4. `TokenRecordingModelWrapper` 包裹 token 用量追踪
5. `RetryChatModel` 包裹速率限制重试

### 视频块往返策略

视频块在 OpenAI 系列 formatter 无法原生处理时使用占位符 round-trip：

1. `_substitute_video_blocks` — 发送前用占位符替换
2. `_replace_video_placeholders` — 格式化阶段
3. `_restore_video_blocks` — 接收后恢复
4. `_promote_tool_result_videos` — 工具结果中的视频镜像（与 `promote_tool_result_images` 同理）

### 消息重排

`_reorder_tool_and_promoted_messages` — OpenAI API 要求所有 tool-result 消息必须紧随在 assistant 消息后，不能与其他消息交错。

### thinking_param_style alignment

`reasoning_content` 注入：格式化后统计预期 assistant 消息数量，跳过被 formatter 丢弃的 `thinking`、`file` 类型，然后 1:1 注入 `reasoning_content`。

### RoutingChatModel

`RoutingChatModel(local_endpoint, cloud_endpoint, routing_cfg)`:

- 继承 agentscope `ChatModelBase`
- 所有调用路由到选定端点
- `RoutingPolicy.decide(text, channel, tools_available)` — `"cloud_first"` → cloud；其他 → local

---

## 14. 统计与可观测性

路径：`src/qwenpaw/agent_stats/service.py`、`observability/`、`token_usage/`

### AgentStatsService.get_summary(workspace_dir, start_date, end_date)

返回 `AgentStatsSummary`:

1. 读 `workspace_dir/chats.json`（via `JsonChatRepository.list_chats()`）统计日聊天数
2. 扫描 `workspace_dir/sessions/*.json`（按频道子目录），用 mtime + content range 优化
3. 每个 session 统计 `user_messages` / `assistant_messages` / `tool_calls`、按日 / 按频道
4. 合并 `TokenUsageManager.get_summary()` 得到 `prompt_tokens` / `completion_tokens` / `llm_calls`

### 其他观测能力

- `LangfuseToolSpanMiddleware` — 工具执行跨度
- `observability/langfuse.py` — Langfuse trace 集成
- `TokenRecordingModelWrapper` (token_usage) — 每调用 token 记录
- `Envelope MODEL_CALL_END` 事件 — 线级 token 捕获
- `ResourceGovernor` — 资源消耗审计

---

## 15. 关键架构模式总结

| 模式 | 文件 | 用途 |
|------|------|------|
| 构造器注入 | `react_agent.py` + `runtime/builder.py` | Agent 不自建子对象；由 AgentBuilder 统一注入 |
| 装饰器自注册 | `runtime/tool_registry.py` | `@tool_descriptor` 装饰器执行时写入全局 registry |
| 优先级排序贡献者 | `runtime/prompt_manager.py` | 系统提示词按 priority 升序拼接 |
| Hook 拓扑排序 | `runtime/hooks.py` (`_topo_sort`) | 钩子 before/after 约束循环依赖检测 |
| 门面权限引擎 | `runtime/tool_guard.py` + `security/tool_guard/` | GuardedFunctionTool + YAML regex 签名匹配 |
| 策略路由 | `agents/routing_chat_model.py` | 按 RoutingPolicy 路由 local/cloud 双端点 |
| 沙箱策略 | `sandbox/` | Seatbelt / Bubblewrap / Landlock / AppContainer / NONE 统一接口 |
| 中间件管道 | `agents/middlewares.py` | 系统 prompt / 模型调用 / 工具结果 / 上下文压缩 |
| 延迟门控执行 | `loop/gates/runner.py` (`apply_stop_result`/`check_pending_gates`) | 工具调用中 STOP 信号延后到下一轮 `_reasoning` 入口消费 |
| 技能目录合并 | `agents/skill_system/registry.py` | builtin / customized / 用户自建三类合并 |
| 3 源技能池 | `skill_system/` | Packaged builtin ↔ skill_pool ↔ workspace skills 同步 |
| Loop Engineering Gate 体系 | `loop/gates/`, `loop/gates/runner.py` | 优先级排序的 StopHandler + 多维度 StopGate + scope 隔离 |

---

## 16. 新增组件的 3 个常见位置

### 新工具

1. 写函数 + `@tool_descriptor(...)`
2. 在 `agents/tools/__init__.py` 导入（触发装饰器执行）
3. 可选在 `runtime/tool_guard/` 添加 YAML 规则定义

### 新 AgentMode 子类

1. 继承 `modes/base.py` 的 `AgentMode`
2. 实现 `setup()` / `commands()` / `tools()` / `hooks()` / `prompt_contributors()` / `is_active(ctx)`
3. 在 `WorkspacePlugins.active_mode_names` 注册或调用时启用

### 新 Hook

1. 继承 `HookBase`（`runtime/hooks.py`）
2. 在合适的 phase 上设置 `phase`/`priority`/`before`/`after`
3. 通过 `HookRegistry.register(hook)` 注册（通常在 `WorkspacePlugins` 构造或 Mode.setup 内）

### 新 Loop 插件（自定义 Gate）

1. 继承 `LoopGate`（session 隔离）或 `FileLoopGate`（文件状态基类，`loop/gates/`）
2. 实现 `name`/`check()`，可选 `build_continuation()`/`reset()`
3. 在 `PluginAPI.on_load()` 中：
   ```python
   handler = get_or_create_stop_handler(self.workspace, scope="my-scope")
   handler.register(MyGate())
   self.register_agent_stop_handler(
       handler=handler, priority=100, name="my-gate", scope="my-scope",
   )
   ```
4. 通过 `StopHandlerRegistration.scope` 控制与其他模式的隔离

---

## 附：文件路径速查

```
src/qwenpaw/
├── app/                        # FastAPI 后端 + 工作区管理
│   ├── _app.py                 # DynamicMultiAgentRunner
│   ├── routers/                # REST API 路由
│   ├── channels/               # 消息通道
│   ├── workspace/              # Workspace / ServiceManager
│   ├── workspace_registry.py   # WorkspaceRegistry
│   ├── crons/                  # 定时任务
│   └── mcp/                    # MCP 协议集成
├── agents/
│   ├── react_agent.py          # QwenPawAgent 主类
│   ├── middlewares.py          # MemoryMiddleware / ToolResultPruningMiddleware
│   ├── model_factory.py        # 模型 + formatter 构造
│   ├── routing_chat_model.py   # local/cloud 路由
│   ├── offloader.py            # 展开 offload 的子任务
│   ├── prompt_builder.py       # 系统提示词（锚点 + 插件 双模型）
│   ├── hooks/                  # BootstrapHook
│   ├── memory/                 # BaseMemoryManager + ADBPG/ReMeLight/Noop
│   ├── skill_system/           # 技能池与 workspace 服务
│   ├── skills/                 # 内置技能目录
│   ├── tools/                  # 内置工具函数
│   ├── context/                # 上下文压缩
│   └── acp/                    # Agent Client Protocol
├── runtime/
│   ├── runtime.py              # 8 阶段 Runtime.run()
│   ├── builder.py              # AgentBuilder.build()
│   ├── executor.py             # AgentExecutor
│   ├── envelope.py             # SSE envelope 状态机
│   ├── phases.py               # Phase 枚举
│   ├── hooks.py                # HookBase / HookContext / HookAction
│   ├── tool_registry.py        # @tool_descriptor / ToolRegistry
│   ├── tool_guard.py           # GuardedFunctionTool
│   ├── prompt_manager.py       # PromptContributor / PromptManager
│   ├── prompt_contributors.py  # 8 个内置 prompt 贡献者
│   └── tool_calls.py           # ToolCoordinator / HITL
├── loop/
│   ├── __init__.py             # 导出公共 API（StopHandler/Gates/Rubric）
│   ├── react_gates.py          # ReAct 模式默认 gate 注册
│   ├── handler_registry.py     # get_or_create_stop_handler 工厂
│   └── gates/
│       ├── base.py             # StopAction/StopGate/StopHandlerResult/StopHandlerRegistration
│       ├── loop_gate.py        # LoopGate 抽象基类（session 隔离）
│       ├── handler.py          # StopHandler 表决逻辑
│       ├── runner.py           # run_stop_handlers / apply_stop_result / check_pending_gates
│       ├── iteration.py        # IterationGate（优先级 10）
│       ├── budget.py           # BudgetGate（优先级 20）
│       ├── doom_loop.py        # DoomLoopGate（优先级 5，多级升级）
│       ├── rubric.py           # RubricGate 策略 + StandaloneRubricGate
│       └── file_loop_gate.py   # FileLoopGate 文件状态基类
├── modes/
│   ├── base.py                 # AgentMode 协议
│   ├── coding/                 # Coding Mixin + hooks
│   ├── goal/                   # Goal 模式 (GoalMode/GoalSession/3 gates/3 tools)
│   └── mission/                # Mission 模式 (MissionMode/MissionGate/state/hooks)
├── security/
│   ├── tool_guard/             # 工具调用前 YAML regex 规则引擎
│   └── skill_scanner/          # SKILL.md 静态分析
├── governance/                 # 资源治理 / 审计 / 检测
├── sandbox/                    # Seatbelt/Bubblewrap/Landlock/AppContainer/NONE
├── providers/                  # LLM 后端抽象
├── drivers/                    # Driver 抽象层
├── market/                     # Skills Hub
├── config/                     # 配置加载与上下文
├── hooks/                      # Lifecycle 总目录
└── cli/                        # 命令行入口
```
