# Logging in AutoGen (0.7.x) — A Practical Guide

How to capture the events, messages, and tool calls of AutoGen agents (AgentChat and
Core APIs) into a readable log file. Verified against `autogen-core` / `autogen-agentchat`
/ `autogen-ext` **0.7.5**.

---

## 1. Mental model

AutoGen logs through Python's standard `logging` module. There are **no custom loggers** —
you pick one of AutoGen's built-in logger names, attach your own `FileHandler` (and
formatter) to it, and set the level you want.

The three pieces of every setup:

1. **Logger name** — which component's output you want (see table below).
2. **Level** — `INFO` gives you LLM calls; `DEBUG` additionally gives you tool-call and
   speaker-selection lines. This is the #1 reason "nothing shows up".
3. **Handler + Formatter** — where lines go and how they look.

---

## 2. The loggers and what each one emits

AutoGen defines **two** sets of "well-known" logger names — one in `autogen_core`, one in
`autogen_agentchat` — and they are **different strings**. Import the constant from the
package whose output you care about.

| Logger name (string) | Import from | Level | What it emits |
|---|---|---|---|
| `"autogen_core"` | `autogen_core` (plain `getLogger`) | INFO | Runtime activity: "Sending message of type X to Y", "Publishing message..." (only when a runtime/team is used) |
| `"autogen_core.events"` | `autogen_core.EVENT_LOGGER_NAME` | INFO | Structured events as JSON: `LLMCallEvent`, `LLMStreamStart/EndEvent`, `ToolCallEvent`, `MessageEvent` |
| `"autogen_core.trace"` | `autogen_core.TRACE_LOGGER_NAME` | WARNING | Model-client warnings (model not found, encoding fallbacks, unsupported fields) |
| `"autogen_agentchat"` | `autogen_agentchat.TRACE_LOGGER_NAME` | DEBUG | Group-chat internals: "Selected speaker: X" (SelectorGroupChat, MagenticOne) |
| `"autogen_agentchat.events"` | `autogen_agentchat.EVENT_LOGGER_NAME` | DEBUG | Agent events: `ToolCallRequestEvent`, `ToolCallExecutionEvent`, handoffs (AgentChat) |

> **Gotcha:** `TRACE_LOGGER_NAME` from `autogen_core` is `"autogen_core.trace"`, but from
> `autogen_agentchat` it is `"autogen_agentchat"`. A bare `AssistantAgent.run()` emits
> **nothing** to `"autogen_core.trace"` — that logger is mostly warnings + group-chat teams.
> Don't configure it and expect agent output.

### Summary of where the important output actually lives

| You want... | Configure this logger | At level |
|---|---|---|
| Every LLM call (prompt, response, tokens) | `"autogen_core.events"` | `INFO` |
| Tool call requests & executions | `"autogen_agentchat.events"` | `DEBUG` |
| Speaker selection in group chats | `"autogen_agentchat"` | `DEBUG` |
| Message routing between agents (runtime) | `"autogen_core"` | `INFO` |
| Your own milestones (📨/🛠️/🔀) | your own logger (e.g. `"Handoffs Workflow"`) | `INFO` |

---

## 3. One setup to rule them all

Attach a **single shared `FileHandler`** to every logger you care about so all lines land in
one file in time order:

```python
import logging
from autogen_core import EVENT_LOGGER_NAME, TRACE_LOGGER_NAME as CORE_TRACE
from autogen_agentchat import EVENT_LOGGER_NAME as AGENTCHAT_EVENTS, TRACE_LOGGER_NAME as AGENTCHAT_TRACE

LOG_FILE = "agent.log"

# Your custom milestone logger (like app_logger in handoffs_workflow.py)
app_logger = logging.getLogger("My Workflow")
app_logger.setLevel(logging.INFO)
app_logger.propagate = False

# Formatter + handler shared by everyone
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
handler.setFormatter(formatter)

# Attach the same handler to each logger you want captured
for logger in (
    app_logger,
    logging.getLogger("autogen_core"),        # runtime routing (INFO)
    logging.getLogger("autogen_core.events"), # LLM calls (INFO)
    logging.getLogger("autogen_core.trace"),  # model warnings
    logging.getLogger(AGENTCHAT_EVENTS),      # tool calls (DEBUG)
    logging.getLogger(AGENTCHAT_TRACE),       # group-chat selection (DEBUG)
):
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
```

Always set `propagate = False` so records don't also bubble up to the root logger and spam
the console.

---

## 4. Reading the structured events

Events on `"autogen_core.events"` are logged as **JSON strings**. A typical LLM call line:

```json
[2026-07-31 10:00:00] [INFO] {"type": "LLMCall", "messages": [{"role": "user", "content": "..."}],
"response": {"content": "..."}, "prompt_tokens": 120, "completion_tokens": 45,
"agent_id": "assistant", "tools": [...]}
```

Available structured events (in `autogen_core.logging`):

| Event | `type` | Key fields |
|---|---|---|
| `LLMCallEvent` | `LLMCall` | `messages`, `response`, `prompt_tokens`, `completion_tokens` |
| `LLMStreamStartEvent` | `LLMStreamStart` | `messages` |
| `LLMStreamEndEvent` | `LLMStreamEnd` | `response`, `prompt_tokens`, `completion_tokens` |
| `ToolCallEvent` | `ToolCall` | `tool_name`, `arguments`, `result` |
| `MessageEvent` | `Message` | `payload`, `sender`, `receiver`, `kind`, `delivery_stage` |
| `MessageDroppedEvent` | `MessageDropped` | `payload`, `sender`, `receiver`, `kind` |

They are emitted by the model clients (e.g. the OpenAI client) and by `BaseTool` — you
don't emit them yourself; you just listen.

---

## 5. Logging your own milestones

Use a dedicated logger (the `app_logger` pattern from `POC/02_Orchestrator_2_Agents/handoffs_workflow.py`):

```python
app_logger.info(f"📨 [{ctx.sender.type}] → [{self.id.type}]:\n\t{last_msg}")     # incoming
app_logger.info(f"🛠️ [{self.id.type}] | TOOL: [{call.name}], Args: [{arguments}]")  # tool run
app_logger.info(f"🔀 [DELEGATION] {self.id.type} ➔ {topic_type}")                   # handoff
app_logger.info(f"📨 [{self.id.type}] → [User]:\n\t{llm_result.content}")           # reply
```

If you don't want a separate logger, you can reuse an AutoGen one — but note the two
worlds:
- Using the **core** trace logger means your milestones interleave with model warnings.
- Using your **own** logger means your milestones are clean, and AutoGen internals can be
  toggled independently by level. This is usually the better choice.

---

## 6. Capturing messages

### a) The returned `TaskResult` (no logging needed)

`agent.run()` already returns everything as objects — this is the *cleanest* source for
messages:

```python
result = asyncio.run(agent.run(task="Find information on AutoGen"))
for m in result.messages:
    print(type(m).__name__, "|", getattr(m, "source", "?"), "|", m.content)
```

### b) Streaming events (for `run_stream()`)

`run_stream()` yields each inner event as it happens — log them as they occur:

```python
import asyncio
from autogen_agentchat.messages import (
    ToolCallRequestEvent, ToolCallExecutionEvent, TextMessage,
)

async def main():
    async for event in agent.run_stream(task="Find information on AutoGen"):
        if isinstance(event, TextMessage):
            app_logger.info(f"💬 [{event.source}]: {event.content}")
        elif isinstance(event, ToolCallRequestEvent):
            app_logger.info(f"🛠️ REQUEST {event.content}")
        elif isinstance(event, ToolCallExecutionEvent):
            app_logger.info(f"✅ RESULT {event.content}")

asyncio.run(main())
```

### c) Runtime message routing

Only fires when a **runtime/team** is used (e.g. `SingleThreadedAgentRuntime`, `RoundRobinGroupChat`):
`"autogen_core"` at INFO gives "Sending message of type ..." / "Publishing message ...",
and `"autogen_core.events"` gives structured `MessageEvent`s.

---

## 7. Capturing tool calls

Three ways, depending on how deep you want to go:

| Method | What you get | Setup |
|---|---|---|
| `ToolCallEvent` (core) | tool name + JSON args + string result, per execution | `"autogen_core.events"` at `INFO` |
| `ToolCallRequestEvent` / `ToolCallExecutionEvent` (AgentChat) | full request + per-result messages with content | `"autogen_agentchat.events"` at `DEBUG` |
| `run_stream()` inspection | the event objects directly in code | iterate `run_stream()` and log |

AgentChat logs tool calls at **DEBUG** on `"autogen_agentchat.events"` — this is why `INFO`
alone misses them:

```python
logging.getLogger("autogen_agentchat.events").setLevel(logging.DEBUG)
```

Example line you'd see:

```
[2026-07-31 10:00:00] [DEBUG] ToolCallRequestEvent(source='assistant', content=[FunctionCall(id='call_1', arguments='{"query":"AutoGen"}', name='web_search')], models_usage=RequestUsage(prompt_tokens=100, completion_tokens=10))
```

---

## 8. Level cheat-sheet

| Logger | `INFO` gives | `DEBUG` adds |
|---|---|---|
| `"autogen_core"` | runtime send/publish | — |
| `"autogen_core.events"` | LLM calls, tool results, messages | — |
| `"autogen_core.trace"` | nothing much | — |
| `"autogen_agentchat"` | nothing | speaker selection (group chats) |
| `"autogen_agentchat.events"` | nothing | tool call request/execution, handoffs |

---

## 9. Minimal recipe (for `logtest.py`-style single-agent scripts)

```python
import logging
from autogen_core import EVENT_LOGGER_NAME
from autogen_agentchat import EVENT_LOGGER_NAME as AGENTCHAT_EVENTS

file_handler = logging.FileHandler("log.log", encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)

for name, level in (("autogen_core.events", logging.INFO), (AGENTCHAT_EVENTS, logging.DEBUG)):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(file_handler)
```

Run the agent, then read `log.log`: you'll see one JSON `LLMCall` line per model call
(prompt/response/tokens) and `ToolCallRequestEvent`/`ToolCallExecutionEvent` lines for each
tool execution.

---

## 10. Common gotchas

- **`getlogger` ≠ `getLogger`** — Python is case-sensitive; `logging.getlogger(...)` raises
  `AttributeError`.
- **`TRACE_LOGGER_NAME` is a variable, not a string** — `logging.getLogger("TRACE_LOGGER_NAME")`
  creates a logger with that literal name; AutoGen never logs to it. Pass the imported constant.
- **Two packages, two constant values** — `autogen_core.TRACE_LOGGER_NAME == "autogen_core.trace"`
  but `autogen_agentchat.TRACE_LOGGER_NAME == "autogen_agentchat"`. Import from the package
  that owns the output you want.
- **INFO hides DEBUG** — AgentChat tool-call and speaker-selection lines are `DEBUG`; set
  `DEBUG` explicitly or they silently vanish.
- **`propagate = False`** on every logger you configure, or records also reach the root
  logger (console output / double-writing).
- **Empty log file = wrong logger or wrong level**, not a broken script. Check the table in
  §2 before debugging.
- **A bare `AssistantAgent.run()` uses no runtime** — `"autogen_core"` routing lines and
  `MessageEvent`s appear only when you run an actual runtime or a team.
