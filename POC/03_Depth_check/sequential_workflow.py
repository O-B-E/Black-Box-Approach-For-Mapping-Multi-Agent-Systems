"""Sequential workflow (AutoGen Core).

Based on https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/design-patterns/sequential-workflow.html
but instead of one hand-written class per agent, a single generic `WorkerAgent`
is configured from the WORKFLOW list in workers.py, and the topic chain
(worker[i] -> worker[i+1] -> User) is built from that list's order.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from autogen_core import (
    FunctionCall,
    MessageContext,
    RoutedAgent,
    SingleThreadedAgentRuntime,
    TopicId,
    TypeSubscription,
    message_handler,
)
from autogen_core.models import (
    ChatCompletionClient,
    FunctionExecutionResult,
    LLMMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv

from workers import WORKFLOW, Worker

LOG_FILE = "sequential_agents.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Sequential Agents")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)

USER_TOPIC = "User"

# Long chains hit the Gemini free-tier limit (15 requests/minute) part-way through, so each
# hop can be made to wait before it calls the model. 0 keeps the original timing.
HOP_DELAY_SECONDS = float(os.getenv("HOP_DELAY_SECONDS", "0"))


@dataclass
class Message:
    content: str


class WorkerAgent(RoutedAgent):
    """A generic step in the chain: runs the LLM (with its tools) and forwards the result."""

    def __init__(self, model_client: ChatCompletionClient, worker: Worker, next_topic: str) -> None:
        super().__init__(f"{worker.name} worker")
        self._model_client = model_client
        self._system_message = SystemMessage(content=worker.system_message)
        self._next_topic = next_topic
        self._tools = {
            tool.name: tool
            for tool in (FunctionTool(fn, description=fn.__doc__ or fn.__name__) for fn in worker.tools)
        }

    @message_handler
    async def handle_message(self, message: Message, ctx: MessageContext) -> None:
        if HOP_DELAY_SECONDS:
            await asyncio.sleep(HOP_DELAY_SECONDS)

        context: list[LLMMessage] = [
            self._system_message,
            UserMessage(content=message.content, source="previous_step"),
        ]

        result = await self._model_client.create(
            context,
            tools=list(self._tools.values()),
            cancellation_token=ctx.cancellation_token,
        )

        # Gemini rejects function-call messages sent back to it (they lose their thought_signature through
        # the OpenAI-compatible API), so, like concurrent_agents.py, tool results go back as plain text and
        # the follow-up call has no tools, which makes the model answer with text.
        if isinstance(result.content, list):
            tool_results = [await self._run_tool(call, ctx) for call in result.content]
            results_text = "\n".join(f"Tool '{res.name}' returned: {res.content}" for res in tool_results)
            context.append(
                UserMessage(
                    content=f"[System Note: Tool execution completed with results:\n{results_text}\n"
                    "Please complete your task based on these results.]",
                    source="System",
                )
            )
            result = await self._model_client.create(context, cancellation_token=ctx.cancellation_token)

        assert isinstance(result.content, str)
        logger.info(f"📨 [{self.id.type}] → [{self._next_topic}]:\n\t{result.content}")
        await self.publish_message(Message(result.content), topic_id=TopicId(self._next_topic, source=self.id.key))

    async def _run_tool(self, call: FunctionCall, ctx: MessageContext) -> FunctionExecutionResult:
        arguments = call.arguments
        try:
            if call.name not in self._tools:
                raise ValueError(f"Unknown tool: {call.name}")
            tool = self._tools[call.name]
            arguments = json.loads(call.arguments)
            output = await tool.run_json(arguments, ctx.cancellation_token)
            result_as_str = tool.return_value_as_string(output)
            logger.info(f"🛠️ [{self.id.type}] TOOL: [{call.name}], Args: [{arguments}], Result: {result_as_str}")
            return FunctionExecutionResult(call_id=call.id, name=call.name, content=result_as_str, is_error=False)
        except Exception as e:
            logger.info(f"❌ [{self.id.type}] TOOL: [{call.name}], Args: [{arguments}], Error: {e}")
            return FunctionExecutionResult(call_id=call.id, name=call.name, content=str(e), is_error=True)


class UserAgent(RoutedAgent):
    """End of the chain: receives the final output."""

    def __init__(self) -> None:
        super().__init__("Receives the final output of the workflow.")

    @message_handler
    async def handle_final(self, message: Message, ctx: MessageContext) -> None:
        sender = ctx.sender.type if ctx.sender else "Workflow"
        print(f"\n[{sender}]:\n{message.content}")


async def build_runtime(model_client: ChatCompletionClient, workflow: list[Worker]) -> SingleThreadedAgentRuntime:
    names = [w.name for w in workflow]
    if len(set(names)) != len(names) or USER_TOPIC in names:
        raise ValueError(f"Worker names must be unique and not '{USER_TOPIC}': {names}")

    runtime = SingleThreadedAgentRuntime()
    next_topics = names[1:] + [USER_TOPIC]

    for worker, next_topic in zip(workflow, next_topics):
        await WorkerAgent.register(
            runtime,
            type=worker.name,
            factory=lambda w=worker, n=next_topic: WorkerAgent(model_client, w, n),
        )
        await runtime.add_subscription(TypeSubscription(topic_type=worker.name, agent_type=worker.name))

    await UserAgent.register(runtime, type=USER_TOPIC, factory=UserAgent)
    await runtime.add_subscription(TypeSubscription(topic_type=USER_TOPIC, agent_type=USER_TOPIC))
    return runtime


async def main() -> None:
    load_dotenv()
    model_client = OpenAIChatCompletionClient(
        model=os.getenv("MODEL_NAME"),
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model_info={
            "vision": True,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": "unknown",
        },
    )

    runtime = await build_runtime(model_client, WORKFLOW)
    flow = " → ".join([USER_TOPIC] + [w.name for w in WORKFLOW] + [USER_TOPIC])

    while True:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id == "exit":
            break

        user_input = input("Enter a task(prompt): ")
        if user_input == "exit":
            break

        logger.info(f"{'-'*32} SESSION  START {'-'*32}\nSession ID: {session_id}\nFlow: {flow}\n")

        runtime.start()
        logger.info(f"📨 [{USER_TOPIC}] → [{WORKFLOW[0].name}]:\n\t{user_input}")
        await runtime.publish_message(
            Message(content=user_input), topic_id=TopicId(WORKFLOW[0].name, source=session_id)
        )
        await runtime.stop_when_idle()

        logger.info(f"{'-'*32} SESSION  END {'-'*32}\n\n")

    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
