"""Handoffs workflow (AutoGen Core).

Based on ../02_Orchestrator_2_Agents/handoffs_workflow.py, but instead of one
hand-written agent per role, a single generic `WorkerAgent` is configured from the
WORKFLOW list in workers.py, and the handoff chain (worker[i] -> worker[i+1] -> User)
is built from that list's order.

The difference from sequential_workflow.py is what travels: there each worker only sees
the previous worker's output, here every worker is handed the whole conversation so far
and moves it on by calling its `transfer_to_<next>` tool.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import List, Tuple

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
    AssistantMessage,
    ChatCompletionClient,
    FunctionExecutionResult,
    LLMMessage,
    ModelInfo,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool, Tool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv
from pydantic import BaseModel

from workers import WORKFLOW, Worker

LOG_FILE = "handoffs_workflow.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Handoffs Agents")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)

USER_TOPIC = "User"

# Long chains hit the Gemini free-tier limit (15 requests/minute) part-way through, so each
# worker can be made to wait before it calls the model. 0 keeps the original timing.
HOP_DELAY_SECONDS = float(os.getenv("HOP_DELAY_SECONDS", "0"))

HANDOFF_INSTRUCTION = (
    "When your part of the work is done, call `{tool}` to hand everything over to the next worker. "
    "Do not reply to the user yourself."
)
FINAL_INSTRUCTION = "You are the last worker in the pipeline. Reply to the user directly."


class UserLogin(BaseModel):
    pass


class UserTask(BaseModel):
    context: List[LLMMessage]


class AgentResponse(BaseModel):
    reply_to_topic_type: str
    context: List[LLMMessage]


def make_handoff_tool(target: str) -> FunctionTool:
    """Build the delegate tool that hands the conversation to `target`."""

    def transfer() -> str:
        return target

    return FunctionTool(
        transfer,
        name=f"transfer_to_{target}",
        description=f"Hand the work over to {target}, the next worker in the pipeline.",
    )


class WorkerAgent(RoutedAgent):
    """A generic step in the chain: runs the LLM (with its tools) and hands off to the next worker."""

    def __init__(
        self,
        model_client: ChatCompletionClient,
        worker: Worker,
        next_worker: str | None,
        user_topic: str,
    ) -> None:
        super().__init__(f"{worker.name} worker")
        self._model_client = model_client
        self._agent_topic_type = worker.name
        self._user_topic_type = user_topic

        handoff = HANDOFF_INSTRUCTION.format(tool=f"transfer_to_{next_worker}") if next_worker else FINAL_INSTRUCTION
        self._system_message = SystemMessage(content=f"{worker.system_message} {handoff}")

        tools: List[Tool] = [
            FunctionTool(fn, description=fn.__doc__ or fn.__name__) for fn in worker.tools
        ]
        self._tools = {tool.name: tool for tool in tools}
        self._tool_schema = [tool.schema for tool in tools]

        delegate_tools: List[Tool] = [make_handoff_tool(next_worker)] if next_worker else []
        self._delegate_tools = {tool.name: tool for tool in delegate_tools}
        self._delegate_tool_schema = [tool.schema for tool in delegate_tools]

    @message_handler
    async def handle_task(self, message: UserTask, ctx: MessageContext) -> None:
        # Incoming messages are logged by whoever sent them (UserAgent for user turns, the
        # 🔀 handoff line for worker turns), so logging them again here only duplicates.
        if HOP_DELAY_SECONDS:
            await asyncio.sleep(HOP_DELAY_SECONDS)

        llm_result = await self._model_client.create(
            messages=[self._system_message] + message.context,
            tools=self._tool_schema + self._delegate_tool_schema,
            cancellation_token=ctx.cancellation_token,
        )

        while isinstance(llm_result.content, list) and all(isinstance(m, FunctionCall) for m in llm_result.content):
            # A model that writes a reply and calls a tool in the same turn has its text put in
            # `thought`, not `content`. Keep it: on the handoff path it is the only text the worker
            # ever produces, and appending it is what lets a list travel down the chain.
            if llm_result.thought:
                logger.info(f"💬 [{self.id.type}]:\n\t{llm_result.thought}")
                message.context.append(AssistantMessage(content=llm_result.thought, source=self.id.type))

            tool_results: List[FunctionExecutionResult] = []
            handoff_targets: List[str] = []

            for call in llm_result.content:
                if call.name in self._tools:
                    tool_results.append(await self._run_tool(call, ctx))
                elif call.name in self._delegate_tools:
                    result = await self._delegate_tools[call.name].run_json({}, ctx.cancellation_token)
                    handoff_targets.append(self._delegate_tools[call.name].return_value_as_string(result))
                else:
                    # The model sometimes garbles its own handoff tool name (e.g. transfer_to_SEOSPosition).
                    # Feed the error back as a tool result instead of raising, so the chain survives and the
                    # worker can retry with a valid name.
                    known = list(self._tools) + list(self._delegate_tools)
                    error = f"Unknown tool: {call.name}. Available tools: {known}"
                    logger.info(f"❌ [{self.id.type}] UNKNOWN TOOL: [{call.name}], expected one of {known}")
                    tool_results.append(
                        FunctionExecutionResult(call_id=call.id, name=call.name, content=error, is_error=True)
                    )

            # Gemini rejects function-call messages sent back to it (they lose their thought_signature
            # through the OpenAI-compatible API), so, like sequential_workflow.py, tool results go back
            # as plain text.
            if tool_results:
                results_text = "\n".join(f"Tool '{res.name}' returned: {res.content}" for res in tool_results)
                message.context.append(
                    UserMessage(
                        content=f"[System Note: Tool execution completed with results:\n{results_text}\n"
                        "Please complete your task based on these results.]",
                        source="System",
                    )
                )

            if handoff_targets:
                await self._hand_off(handoff_targets, message.context)
                return

            if not tool_results:
                return

            # The follow-up call keeps only the handoff tool, so the worker either moves the chain on
            # or answers with text, instead of calling its own tools again.
            llm_result = await self._model_client.create(
                messages=[self._system_message] + message.context,
                tools=self._delegate_tool_schema,
                cancellation_token=ctx.cancellation_token,
            )

        assert isinstance(llm_result.content, str)
        print(f"\n[{self.id.type}]:\n{llm_result.content}\n")
        logger.info(f"📨 [{self.id.type}] → [{self._user_topic_type}]:\n\t{llm_result.content}")

        message.context.append(AssistantMessage(content=llm_result.content, source=self.id.type))
        await self.publish_message(
            AgentResponse(context=message.context, reply_to_topic_type=self._agent_topic_type),
            topic_id=TopicId(self._user_topic_type, source=self.id.key),
        )

    async def _hand_off(self, targets: List[str], context: List[LLMMessage]) -> None:
        for target in targets:
            logger.info(f"🔀 [{self.id.type}] → [{target}] HANDOFF, context: {len(context)} messages")
            delegate_context = list(context) + [
                UserMessage(
                    content=f"[System: Transferred to {target}. Please continue the work.]",
                    source="System",
                )
            ]
            await self.publish_message(
                UserTask(context=delegate_context), topic_id=TopicId(target, source=self.id.key)
            )

    async def _run_tool(self, call: FunctionCall, ctx: MessageContext) -> FunctionExecutionResult:
        arguments = call.arguments
        try:
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
    """Start of the chain, and whoever a worker answers to."""

    def __init__(self, user_topic: str, first_worker: str, flow: str) -> None:
        super().__init__("Sends the task in and receives whatever comes back.")
        self._user_topic_type = user_topic
        self._first_worker = first_worker
        self._flow = flow

    @message_handler
    async def handle_user_login(self, message: UserLogin, ctx: MessageContext) -> None:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id.strip().lower() == "exit":
            return

        logger.info(f"{'-'*32} SESSION  START {'-'*32}\nSession ID: {session_id}\nFlow: {self._flow}\n")
        print("Type 'exit' to close the session.")

        user_input = input("Enter a task(prompt): ")
        if user_input.strip().lower() == "exit":
            logger.info(f"{'-'*32} SESSION  END {'-'*32}\n\n")
            return

        logger.info(f"📨 [{USER_TOPIC}] → [{self._first_worker}]:\n\t{user_input}")
        await self.publish_message(
            UserTask(context=[UserMessage(content=user_input, source="User")]),
            topic_id=TopicId(self._first_worker, source=self.id.key),
        )

    @message_handler
    async def handle_task_result(self, message: AgentResponse, ctx: MessageContext) -> None:
        user_input = input("User: ")
        if user_input.strip().lower() == "exit":
            logger.info(f"{'-'*32} SESSION  END {'-'*32}\n\n")
            return

        logger.info(f"📨 [{USER_TOPIC}] → [{message.reply_to_topic_type}]:\n\t{user_input}")
        message.context.append(UserMessage(content=user_input, source="User"))
        await self.publish_message(
            UserTask(context=message.context),
            topic_id=TopicId(message.reply_to_topic_type, source=self.id.key),
        )


async def build_runtime(model_client: ChatCompletionClient, workflow: list[Worker]) -> SingleThreadedAgentRuntime:
    names = [w.name for w in workflow]
    if len(set(names)) != len(names) or USER_TOPIC in names:
        raise ValueError(f"Worker names must be unique and not '{USER_TOPIC}': {names}")

    runtime = SingleThreadedAgentRuntime()
    next_workers: list[str | None] = names[1:] + [None]
    flow = " → ".join([USER_TOPIC] + names + [USER_TOPIC])

    for worker, next_worker in zip(workflow, next_workers):
        await WorkerAgent.register(
            runtime,
            type=worker.name,
            factory=lambda w=worker, n=next_worker: WorkerAgent(model_client, w, n, USER_TOPIC),
        )
        await runtime.add_subscription(TypeSubscription(topic_type=worker.name, agent_type=worker.name))

    await UserAgent.register(
        runtime,
        type=USER_TOPIC,
        factory=lambda: UserAgent(USER_TOPIC, names[0], flow),
    )
    await runtime.add_subscription(TypeSubscription(topic_type=USER_TOPIC, agent_type=USER_TOPIC))
    return runtime


async def main() -> None:
    load_dotenv()
    model_client = OpenAIChatCompletionClient(
        model=os.getenv("MODEL_NAME"),
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model_info=ModelInfo(
            vision=True,
            function_calling=True,
            json_output=True,
            structured_output=True,
            family="unknown",
        ),
    )

    runtime = await build_runtime(model_client, WORKFLOW)

    runtime.start()
    await runtime.publish_message(UserLogin(), topic_id=TopicId(USER_TOPIC, source=str(uuid.uuid4())))
    await runtime.stop_when_idle()
    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
