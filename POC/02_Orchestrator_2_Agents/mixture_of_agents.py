import asyncio
import json
from dataclasses import dataclass
from pyexpat import model
from typing import List
import os
import dotenv
import math
import logging

from autogen_core import AgentId, FunctionCall, MessageContext, RoutedAgent, SingleThreadedAgentRuntime, message_handler
from autogen_core.models import (
    ChatCompletionClient,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool, Tool
from autogen_ext.models.openai import OpenAIChatCompletionClient


LOG_FILE = "mixture_of_agents.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Handoffs Workflow")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)

dotenv.load_dotenv()


@dataclass
class WorkerTask:
    task: str
    previous_results: List[str]


@dataclass
class WorkerTaskResult:
    result: str


@dataclass
class UserTask:
    task: str


@dataclass
class FinalResult:
    result: str


class WorkerAgent(RoutedAgent):
    def __init__(
        self,
        description: str,
        model_client: ChatCompletionClient,
        tools: List[Tool],
        system_message: str = ""
    ) -> None:
        super().__init__(description=description)
        self._model_client = model_client
        self._tool_schema = [tool.schema for tool in tools]
        self._tools = dict([(tool.name, tool) for tool in tools])
        self._system_message = system_message

    @message_handler
    async def handle_task(self, message: WorkerTask, ctx: MessageContext) -> WorkerTaskResult:
        logger.info(f"\n📨 [{ctx.sender.type}] → [{self.id.key}]:\n\t{message.task}")

        if message.previous_results:
            # If previous results are provided, we need to synthesize them to create a single prompt.
            system_prompt = self._system_message + "You have been provided with a set of responses. Synthesize them into a single, high-quality response. Your response should not simply replicate the given answers but should offer a refined, accurate, and comprehensive reply to the instruction. Prioritize tool usage.\n\nResponses from models:"
            system_prompt += "\n" + "\n\n".join([f"{i+1}. {r}" for i, r in enumerate(message.previous_results)])
            context = [
                SystemMessage(content=system_prompt),
                UserMessage(content=message.task, source="user"),
            ]
        else:
            # If no previous results are provided, we can simply pass the user query to the model.
            system_prompt = self._system_message + "You have been provided with a query. Your response should be accurate, comprehensive, and prioritize tool usage."
            context = [SystemMessage(content=system_prompt), UserMessage(content=message.task, source="user")]

        llm_result = await self._model_client.create(
            context,
            tools=self._tool_schema,
            cancellation_token=ctx.cancellation_token,
        )

        while isinstance(llm_result.content, list) and all(isinstance(m, FunctionCall) for m in llm_result.content):
            tool_call_results: List[FunctionExecutionResult] = []

            for call in llm_result.content:
                if call.name not in self._tools:
                    raise ValueError(f"Unknown tool: {call.name}")

                arguments = json.loads(call.arguments)
                result = await self._tools[call.name].run_json(arguments, ctx.cancellation_token)
                result_as_str = self._tools[call.name].return_value_as_string(result)

                logger.info(f"🛠️ [{self.id.key}] TOOL: [{call.name}], Args: [{arguments}], Result: {result_as_str}")

                tool_call_results.append(
                    FunctionExecutionResult(call_id=call.id, content=result_as_str, is_error=False, name=call.name)
                )

            # 1. Format tool results into simple readable context text
            results_text = "\n".join([f"Tool '{res.name}' returned: {res.content}" for res in tool_call_results])

            # 2. Add plain text message to context instead of structured function objects
            context.append(
                UserMessage(
                    content=f"[System Note: Tool execution completed with results:\n{results_text}\nPlease respond to the user based on these results.]",
                    source="System",
                )
            )

            # 3. Call LLM as a standard conversational turn (without tool schema if you want to stop re-triggering tools)
            llm_result = await self._model_client.create(
                context,
                cancellation_token=ctx.cancellation_token,
            )

        assert isinstance(llm_result.content, str)
        logger.info(f"📨 [{self.id.type}] → [{ctx.sender.type}]:\n\t{llm_result.content}")
        return WorkerTaskResult(result=llm_result.content)


class OrchestratorAgent(RoutedAgent):
    def __init__(
        self,
        model_client: ChatCompletionClient,
        worker_agent_types: List[List[str]],
        num_layers: int,
    ) -> None:
        super().__init__(description="Aggregator Agent")
        self._model_client = model_client
        self._worker_agent_types = worker_agent_types
        self._num_layers = num_layers

    @message_handler
    async def handle_task(self, message: UserTask, ctx: MessageContext) -> FinalResult:
        logger.info(f"\n📨 [User] → [{self.id.type}]:\n\t{message.task}")
        # Create task for the first layer.
        worker_task = WorkerTask(task=message.task, previous_results=[])
        # Iterate over layers.
        for i in range(self._num_layers):
            # Assign workers for this layer.
            worker_ids = [
                AgentId(worker_type, f"layer {i}/worker {j}")
                for j, worker_type in enumerate(self._worker_agent_types[i])
            ]
            # Dispatch tasks to workers.
            logger.info(f"🔀 [{self.id.type}]: Dispatch to workers at layer {i}")
            results = await asyncio.gather(*[self.send_message(worker_task, worker_id) for worker_id in worker_ids])
            logger.info(f"\n🔀 [{self.id.type}]: Received results from workers at layer {i}")
            # Prepare task for the next layer.
            worker_task = WorkerTask(task=message.task, previous_results=[r.result for r in results])
        # Perform final aggregation.
        logger.info(f"📝 [{self.id.type}]: Performing final aggregation")
        system_prompt = "You are an aggregator agent. You are given a set of responses from various experts and you need to synthesize them into a single, high-quality response.\n\nResponses from models:"
        system_prompt += "\n" + "\n\n".join([f"{i+1}. {r}" for i, r in enumerate(worker_task.previous_results)])
        model_result = await self._model_client.create(
            [SystemMessage(content=system_prompt), UserMessage(content=message.task, source="user")]
        )
        assert isinstance(model_result.content, str)

        logger.info(f"📨 [{self.id.type}] → [User]:\n\t{model_result.content}")
        return FinalResult(result=model_result.content)



# Tools
def calculate(expression: str) -> float:
    """
    Evaluates a mathematical expression string and returns the numerical result as a float.

    Use this tool to solve arithmetic, trigonometric, logarithmic, and power expressions.

    Args:
        expression (str): A valid Python mathematical expression to evaluate. 
            Supports standard arithmetic operators (+, -, *, /, **, %) and functions/constants 
            from Python's `math` module (e.g., sin, cos, tan, sqrt, log, pi, e).

    Returns:
        float: The evaluated result of the mathematical expression.

    Examples:
        - Basic arithmetic: "2 + 2 * 5" -> 12.0
        - Exponents & roots: "sqrt(16) + 3 ** 2" -> 13.0
        - Trigonometry: "sin(pi / 2)" -> 1.0
        - Logarithms: "log(100, 10)" -> 2.0
    """
    allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
    return float(eval(expression, {"__builtins__": None}, allowed_names))

def suggest_name(gender: str) -> str:
    """ Suggest a name based on the provided gender male/female."""
    if gender == "male":
        return "John"
    else:
        return "Jane"


calculate_tool = FunctionTool(calculate, description="Perform a calculation based on the provided expression.")

name_tool = FunctionTool(suggest_name, description="Suggest a name based on the provided gender male/female.")


async def main():
    runtime = SingleThreadedAgentRuntime()
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

    await WorkerAgent.register(
        runtime, "calc_worker", lambda: WorkerAgent(description="Calculator Worker",
                                                    model_client=model_client,
                                                    tools=[calculate_tool],
                                                    system_message="You are a calculator assistant. Only answer the parts of the query that require calculations.")
    )
    await WorkerAgent.register(

    runtime, "name_worker", lambda: WorkerAgent(description="Name Suggester Worker",
                                                model_client=model_client,
                                                tools=[name_tool],
                                                system_message="You are a name suggester assistant. Only answer the parts of the query that require suggesting names based on gender.")
    )

    await OrchestratorAgent.register(
        runtime,
        "orchestrator",
        lambda: OrchestratorAgent(model_client=model_client, worker_agent_types=[["calc_worker"], ["name_worker"]], num_layers=2),
    )

    runtime.start()

    while True:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id == "exit":
            break

        user_input = input("Enter a task: ")
        if user_input == "exit":
            break

        logger.info(f"{'-'*32} SESSION  START {'-'*32}\nSession ID: {session_id}\n")

        result = await runtime.send_message(UserTask(task=user_input), AgentId("orchestrator", "default"))

        print(f"Final result:\n{result.result}")
        logger.info(f"{'-'*32} SESSION  END {'-'*32}\n\n")

    await runtime.stop_when_idle()
    await model_client.close()



if __name__ == "__main__":
    asyncio.run(main())