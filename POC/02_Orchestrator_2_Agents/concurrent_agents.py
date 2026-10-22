import asyncio
import json
from dataclasses import dataclass
from typing import List
import os
import dotenv
import logging

from autogen_core import (
    ClosureAgent,
    ClosureContext,
    DefaultTopicId,
    FunctionCall,
    MessageContext,
    RoutedAgent,
    SingleThreadedAgentRuntime,
    TopicId,
    TypeSubscription,
    default_subscription,
    message_handler,
)
from autogen_core.models import (
    ChatCompletionClient,
    FunctionExecutionResult,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool, Tool
from autogen_ext.models.openai import OpenAIChatCompletionClient


LOG_FILE = "concurrent_agents.log"
file_formatter = logging.Formatter("%(message)s")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(file_formatter)

logger = logging.getLogger("Concurrent Agents")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(file_handler)

dotenv.load_dotenv()

RESULTS_TOPIC_TYPE = "worker-results"
COLLECTOR_TYPE = "collector"


@dataclass
class UserTask:
    task: str


@dataclass
class WorkerResult:
    worker: str
    result: str


@default_subscription
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
    async def handle_task(self, message: UserTask, ctx: MessageContext) -> None:
        logger.info(f"\n📨 [User] → [{self.id.type}]:\n\t{message.task}")

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

                logger.info(f"🛠️ [{self.id.type}] TOOL: [{call.name}], Args: [{arguments}], Result: {result_as_str}")

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

            # 3. Call LLM as a standard conversational turn (without tool schema to stop re-triggering tools)
            llm_result = await self._model_client.create(
                context,
                cancellation_token=ctx.cancellation_token,
            )

        assert isinstance(llm_result.content, str)
        logger.info(f"📨 [{self.id.type}] → [User]:\n\t{llm_result.content}")

        # Workers don't see each other: each one publishes its own reply to the results topic.
        await self.publish_message(
            WorkerResult(worker=self.id.type, result=llm_result.content),
            topic_id=TopicId(RESULTS_TOPIC_TYPE, source=self.id.key),
        )


# Tools
EXCHANGE_RATES_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "ILS": 0.27, "JPY": 0.0067}


def convert_currency(amount: float, from_currency: str, to_currency: str) -> float:
    """Convert an amount between currencies (USD, EUR, GBP, ILS, JPY)."""
    usd = amount * EXCHANGE_RATES_TO_USD[from_currency.upper()]
    return round(usd / EXCHANGE_RATES_TO_USD[to_currency.upper()], 2)


UTC_OFFSETS = {"london": 0, "paris": 1, "tel aviv": 2, "new york": -5, "tokyo": 9}


def get_utc_offset(city: str) -> str:
    """Get the UTC offset of a city's time zone."""
    offset = UTC_OFFSETS.get(city.lower())
    if offset is None:
        return f"Unknown city: {city}"
    return f"UTC{offset:+d}"


currency_tool = FunctionTool(convert_currency, description="Convert an amount of money from one currency to another.")

timezone_tool = FunctionTool(get_utc_offset, description="Get the UTC offset of the time zone of a given city.")


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
        runtime, "currency_worker", lambda: WorkerAgent(description="Currency Converter Worker",
                                                        model_client=model_client,
                                                        tools=[currency_tool],
                                                        system_message="You are a currency converter assistant. Only answer the parts of the query that require converting currencies.")
    )
    await WorkerAgent.register(
        runtime, "timezone_worker", lambda: WorkerAgent(description="Time Zone Worker",
                                                        model_client=model_client,
                                                        tools=[timezone_tool],
                                                        system_message="You are a time zone assistant. Only answer the parts of the query that require time zone information.")
    )

    # Collect every worker's reply, the way the user receives them: separately, in arrival order.
    queue = asyncio.Queue[WorkerResult]()

    async def collect_result(_agent: ClosureContext, message: WorkerResult, ctx: MessageContext) -> None:
        await queue.put(message)

    await ClosureAgent.register_closure(
        runtime,
        COLLECTOR_TYPE,
        collect_result,
        subscriptions=lambda: [TypeSubscription(topic_type=RESULTS_TOPIC_TYPE, agent_type=COLLECTOR_TYPE)],
    )

    while True:
        session_id = input("Enter session ID (or 'exit' to quit): ")
        if session_id == "exit":
            break

        user_input = input("Enter a task(prompt): ")
        if user_input == "exit":
            break

        logger.info(f"{'-'*32} SESSION  START {'-'*32}\nSession ID: {session_id}\n")

        runtime.start()
        logger.info("🔀 [User]: Publish task to all workers")
        await runtime.publish_message(UserTask(task=user_input), topic_id=DefaultTopicId())
        await runtime.stop_when_idle()

        while not queue.empty():
            reply = await queue.get()
            print(f"\n[{reply.worker}]:\n{reply.result}")

        logger.info(f"{'-'*32} SESSION  END {'-'*32}\n\n")

    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
