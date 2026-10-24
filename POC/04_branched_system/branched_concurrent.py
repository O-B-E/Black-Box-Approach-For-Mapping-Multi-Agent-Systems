"""Branched network of agents, Concurrent Agents (AutoGen Core).

The *Single Message & Multiple Processors* pattern
(https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/design-patterns/concurrent-agents.html)
applied twice, one inside the other:

    User -> Concierge ==> FlightsWorker
                      ==> HotelsWorker
                      ==> LocalOpsOrchestrator ==> CarRentalWorker
                                               ==> RestaurantWorker
                                               ==> WeatherWorker

`==>` is a single message published to one topic that every child of that node
receives at the same time. Each orchestrator relays the message unchanged, so
the message that reaches the deepest workers is still the original user message.
No agent aggregates: every agent publishes its own reply straight to the results
topic. The collector accumulates all of them and reports the batch as one
message, verbatim - it never reads or merges what the agents wrote.

The tree lives in network.py - this file only turns it into a runtime.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from autogen_core import (
    ClosureAgent,
    ClosureContext,
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

from network import ROOT, Node, walk

LOG_FILE = "branched_concurrent.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Branched Concurrent")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)

RESULTS_TOPIC_TYPE = "node-results"
COLLECTOR_TYPE = "collector"


@dataclass
class Message:
    content: str


@dataclass
class NodeResult:
    agent: str
    depth: int
    result: str


class NodeAgent(RoutedAgent):
    """One agent in the tree.

    Fans the message out to its children (if it has any), then answers it with
    its own tools (if it has any). An orchestrator with a tool does both.
    """

    def __init__(self, model_client: ChatCompletionClient, node: Node, depth: int) -> None:
        role = "orchestrator" if node.is_orchestrator else "worker"
        super().__init__(f"{node.name} ({role})")
        self._model_client = model_client
        self._node = node
        self._depth = depth
        self._system_message = SystemMessage(content=node.system_message)
        self._tools = {
            tool.name: tool
            for tool in (FunctionTool(fn, description=fn.__doc__ or fn.__name__) for fn in node.tools)
        }

    @message_handler
    async def handle_message(self, message: Message, ctx: MessageContext) -> None:
        sender = ctx.sender.type if ctx.sender else "User"
        logger.info(f"\n📨 [{sender}] → [{self.id.type}]:\n\t{message.content}")

        # Fan out first, so the children start working while this agent runs its own LLM.
        # The message is relayed verbatim: one message, several processors.
        if self._node.is_orchestrator:
            children = ", ".join(child.name for child in self._node.children)
            logger.info(f"🔀 [{self.id.type}] → [{self._node.branch_topic}]: {children}")
            await self.publish_message(
                Message(message.content),
                topic_id=TopicId(self._node.branch_topic, source=self.id.key),
            )

        context: list[LLMMessage] = [
            self._system_message,
            UserMessage(content=message.content, source=sender),
        ]

        result = await self._model_client.create(
            context,
            tools=list(self._tools.values()),
            cancellation_token=ctx.cancellation_token,
        )

        # Gemini rejects function-call messages sent back to it (they lose their thought_signature
        # through the OpenAI-compatible API), so, like the earlier POCs, tool results go back as
        # plain text and the follow-up call has no tools, which makes the model answer with text.
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
        logger.info(f"📨 [{self.id.type}] → [User]:\n\t{result.content}")

        # Nobody aggregates: each agent reports straight to the collector.
        await self.publish_message(
            NodeResult(agent=self.id.type, depth=self._depth, result=result.content),
            topic_id=TopicId(RESULTS_TOPIC_TYPE, source=self.id.key),
        )

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


async def build_runtime(
    model_client: ChatCompletionClient, root: Node = ROOT
) -> tuple[SingleThreadedAgentRuntime, list[NodeResult]]:
    """Register one agent per node and wire the fan-out topics from the tree."""
    nodes = walk(root)
    names = [node.name for node in nodes]
    if len(set(names)) != len(names):
        raise ValueError(f"Node names must be unique: {names}")

    runtime = SingleThreadedAgentRuntime()

    # The topic each node listens on: the root listens on its own name, every
    # other node listens on its parent's branch topic together with its siblings.
    inbox = {root.name: root.name}
    depths = {root.name: 0}
    for node in nodes:
        for child in node.children:
            inbox[child.name] = node.branch_topic
            depths[child.name] = depths[node.name] + 1

    for node in nodes:
        await NodeAgent.register(
            runtime,
            type=node.name,
            factory=lambda n=node: NodeAgent(model_client, n, depths[n.name]),
        )
        await runtime.add_subscription(
            TypeSubscription(topic_type=inbox[node.name], agent_type=node.name)
        )

    # The collector accumulates every agent's reply, in arrival order, and the whole
    # batch is reported as one message once the network goes idle.
    collected: list[NodeResult] = []

    async def collect_result(_agent: ClosureContext, message: NodeResult, _ctx: MessageContext) -> None:
        collected.append(message)

    await ClosureAgent.register_closure(
        runtime,
        COLLECTOR_TYPE,
        collect_result,
        subscriptions=lambda: [
            TypeSubscription(topic_type=RESULTS_TOPIC_TYPE, agent_type=COLLECTOR_TYPE)
        ],
    )

    return runtime, collected


def describe(node: Node, indent: int = 0) -> str:
    """The tree as text, for the log header."""
    tool_names = ", ".join(fn.__name__ for fn in node.tools) or "no tools"
    prefix = "    " * indent + ("|-- " if indent else "")
    lines = [f"{prefix}{node.name} [{tool_names}]"]
    lines += [describe(child, indent + 1) for child in node.children]
    return "\n".join(lines)


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

    runtime, collected = await build_runtime(model_client, ROOT)
    tree = describe(ROOT)

    while True:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id == "exit":
            break

        user_input = input("Enter a task(prompt): ")
        if user_input == "exit":
            break

        logger.info(f"{'-' * 32} SESSION  START {'-' * 32}\nSession ID: {session_id}\nNetwork:\n{tree}\n")

        collected.clear()
        runtime.start()
        await runtime.publish_message(
            Message(content=user_input), topic_id=TopicId(ROOT.name, source=session_id)
        )
        await runtime.stop_when_idle()

        # The replies are reported as one batch, exactly as the agents wrote them.
        batch = "".join(
            f"\n\n[{i}] {reply.agent} (depth {reply.depth}):\n{reply.result}"
            for i, reply in enumerate(collected, start=1)
        )
        header = f"{'=' * 28} COLLECTED: {len(collected)} replies {'=' * 28}"
        print(f"\n{header}{batch}")
        logger.info(f"\n📥 [Collector] → [User]: {header}{batch}")

        logger.info(f"{'-' * 32} SESSION  END {'-' * 32}\n\n")

    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
