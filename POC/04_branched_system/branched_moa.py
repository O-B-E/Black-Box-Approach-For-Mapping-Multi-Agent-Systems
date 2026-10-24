"""Branched network of agents, Mixture of Agents (AutoGen Core).

The same tree as branched_concurrent.py, with an upward edge added:

    User -> Concierge --> FlightsWorker
                      --> HotelsWorker
                      --> LocalOpsOrchestrator --> CarRentalWorker
                                               --> RestaurantWorker
                                               --> WeatherWorker

Each orchestrator sends the task to its children (concurrently, by RPC), waits
for all of them, then runs its own LLM over their replies and returns one
synthesis to its own parent. The user gets a single message from the root.

Two things differ from the concurrent variant, and they are the point of this run:
  - The task still travels DOWN unchanged, so the payload reaches depth 2 intact.
  - Replies travel UP, so a child's text is part of its parent's context. The
    list can finally accumulate, and the nesting is visible in the output.

Children's replies are numbered, not named (as in POC 2's mixture_of_agents.py),
so the upward edge carries their text but not their registered names. Any
topology that leaks has to come from what the agents wrote, not from metadata.

The tree lives in network.py - this file only turns it into a runtime.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from autogen_core import (
    AgentId,
    FunctionCall,
    MessageContext,
    RoutedAgent,
    SingleThreadedAgentRuntime,
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

LOG_FILE = "branched_moa.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Branched MoA")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)


@dataclass
class Task:
    task: str
    # The replies of the agents below the recipient, once there are any.
    replies_from_below: list[str] = field(default_factory=list)


@dataclass
class Reply:
    agent: str
    depth: int
    result: str


SYNTHESIS_NOTE = (
    "\n\nYou have been given the replies of the agents reporting to you. Synthesize them into a "
    "single, complete reply. Do not simply repeat them.\n\nReplies from the agents below you:"
)


class NodeAgent(RoutedAgent):
    """One agent in the tree.

    A node with children dispatches to them and synthesizes their replies. A
    node with tools answers with its tools. An orchestrator that has both does
    both, in that order.
    """

    def __init__(self, model_client: ChatCompletionClient, node: Node, depth: int) -> None:
        role = "orchestrator" if node.is_orchestrator else "worker"
        super().__init__(f"{node.name} ({role})")
        self._model_client = model_client
        self._node = node
        self._depth = depth
        self._tools = {
            tool.name: tool
            for tool in (FunctionTool(fn, description=fn.__doc__ or fn.__name__) for fn in node.tools)
        }

    @message_handler
    async def handle_task(self, message: Task, ctx: MessageContext) -> Reply:
        sender = ctx.sender.type if ctx.sender else "User"
        logger.info(f"\n📨 [{sender}] → [{self.id.type}]:\n\t{message.task}")

        # Dispatch to the children and wait for all of them. The task goes down
        # verbatim: one message, several processors, exactly as in the concurrent run.
        replies_from_below: list[str] = []
        if self._node.is_orchestrator:
            children = [child.name for child in self._node.children]
            logger.info(f"🔀 [{self.id.type}] → {children}: dispatch, waiting for all {len(children)}")
            results = await asyncio.gather(
                *[
                    self.send_message(
                        Task(task=message.task),
                        AgentId(child.name, self.id.key),
                        cancellation_token=ctx.cancellation_token,
                    )
                    for child in self._node.children
                ]
            )
            replies_from_below = [result.result for result in results]
            logger.info(f"⬆️ [{self.id.type}]: got {len(replies_from_below)} replies from below")

        system_prompt = self._node.system_message
        if replies_from_below:
            system_prompt += SYNTHESIS_NOTE + "\n" + "\n\n".join(
                f"{i}. {reply}" for i, reply in enumerate(replies_from_below, start=1)
            )

        context: list[LLMMessage] = [
            SystemMessage(content=system_prompt),
            UserMessage(content=message.task, source=sender),
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
        logger.info(f"📨 [{self.id.type}] → [{sender}]:\n\t{result.content}")
        return Reply(agent=self.id.type, depth=self._depth, result=result.content)

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
) -> SingleThreadedAgentRuntime:
    """Register one agent per node. No topics: the tree is walked by RPC."""
    nodes = walk(root)
    names = [node.name for node in nodes]
    if len(set(names)) != len(names):
        raise ValueError(f"Node names must be unique: {names}")

    depths = {root.name: 0}
    for node in nodes:
        for child in node.children:
            depths[child.name] = depths[node.name] + 1

    runtime = SingleThreadedAgentRuntime()
    for node in nodes:
        await NodeAgent.register(
            runtime,
            type=node.name,
            factory=lambda n=node: NodeAgent(model_client, n, depths[n.name]),
        )
    return runtime


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

    runtime = await build_runtime(model_client, ROOT)
    tree = describe(ROOT)
    runtime.start()

    while True:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id == "exit":
            break

        user_input = input("Enter a task(prompt): ")
        if user_input == "exit":
            break

        logger.info(f"{'-' * 32} SESSION  START {'-' * 32}\nSession ID: {session_id}\nNetwork:\n{tree}\n")

        # One call in, one synthesis out: the root's reply is all the user sees.
        reply = await runtime.send_message(
            Task(task=user_input), AgentId(ROOT.name, session_id)
        )

        print(f"\n[{reply.agent}] (final):\n{reply.result}")
        logger.info(f"\n⬆️ [{reply.agent}] → [User] (final):\n{reply.result}")
        logger.info(f"{'-' * 32} SESSION  END {'-' * 32}\n\n")

    await runtime.stop_when_idle()
    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
