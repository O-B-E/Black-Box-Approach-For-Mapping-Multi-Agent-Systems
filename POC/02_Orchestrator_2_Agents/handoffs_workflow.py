import json
import uuid
import os
import asyncio
from datetime import datetime
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
    FunctionExecutionResultMessage,
    LLMMessage,
    ModelInfo,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool, Tool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

LOG_FILE = "handoffs_workflow.log"

def write_log(sender: str, receiver: str, content: str, log_only: bool = True, is_tool: bool = False, session_meta: str = None):
    """
    Handles synchronized logging to stdout and the log file based on routing rules.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. Write session metadata
    if session_meta:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{'-'*80}\n{timestamp}\n{session_meta}\n\n")
        return

    # 2. Console Output Rules: Only show user prompts and agent responses
    if not log_only:
        print(f"\n[{sender} -> {receiver}]: {content}\n\n")

    # 3. Log everything
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if is_tool:
            f.write(f"[{timestamp}] [TOOL EXECUTION] {sender}: {content}\n\n")
        else:
            f.write(f"[{timestamp}] [{sender}] [{receiver}]: {content}\n\n")

class UserLogin(BaseModel):
    pass

class UserTask(BaseModel):
    context: List[LLMMessage]

class AgentResponse(BaseModel):
    reply_to_topic_type: str
    context: List[LLMMessage]

class AIAgent(RoutedAgent):
    def __init__(
        self,
        description: str,
        system_message: SystemMessage,
        model_client: ChatCompletionClient,
        tools: List[Tool],
        delegate_tools: List[Tool],
        agent_topic_type: str,
        user_topic_type: str,
    ) -> None:
        super().__init__(description)
        self._system_message = system_message
        self._model_client = model_client
        self._tools = dict([(tool.name, tool) for tool in tools])
        self._tool_schema = [tool.schema for tool in tools]
        self._delegate_tools = dict([(tool.name, tool) for tool in delegate_tools])
        self._delegate_tool_schema = [tool.schema for tool in delegate_tools]
        self._agent_topic_type = agent_topic_type
        self._user_topic_type = user_topic_type

    @message_handler
    async def handle_task(self, message: UserTask, ctx: MessageContext) -> None:
        llm_result = await self._model_client.create(
            messages=[self._system_message] + message.context,
            tools=self._tool_schema + self._delegate_tool_schema,
            cancellation_token=ctx.cancellation_token,
        )

        while isinstance(llm_result.content, list) and all(isinstance(m, FunctionCall) for m in llm_result.content):
            tool_call_results: List[FunctionExecutionResult] = []
            delegate_targets: List[Tuple[str, UserTask]] = []
            
            for call in llm_result.content:
                arguments = json.loads(call.arguments)
                if call.name in self._tools:
                    write_log(self.id.type, "Tool Engine", f"Calling tool {call.name} with args: {call.arguments}", is_tool=True)
                    result = await self._tools[call.name].run_json(arguments, ctx.cancellation_token)
                    result_as_str = self._tools[call.name].return_value_as_string(result)
                    write_log(self.id.type, "Tool Engine", f"Tool {call.name} Response: {result_as_str}", is_tool=True)
                    
                    tool_call_results.append(
                        FunctionExecutionResult(call_id=call.id, content=result_as_str, is_error=False, name=call.name)
                    )
                elif call.name in self._delegate_tools:
                    result = await self._delegate_tools[call.name].run_json(arguments, ctx.cancellation_token)
                    topic_type = self._delegate_tools[call.name].return_value_as_string(result)

                    delegate_messages = list(message.context) + [
                        UserMessage(content=f"[System: Transferred to {topic_type}. Please assist the user with their request.]", source="System")
                    ]

                    write_log(self.id.type, topic_type, f"Delegated messages: {[msg.content for msg in delegate_messages]}")

                    delegate_targets.append((topic_type, UserTask(context=delegate_messages)))
                else:
                    raise ValueError(f"Unknown tool: {call.name}")

            if len(delegate_targets) > 0:
                for topic_type, task in delegate_targets:
                    await self.publish_message(task, topic_id=TopicId(topic_type, source=self.id.key))

            if len(tool_call_results) > 0:
                message.context.extend(
                    [
                        AssistantMessage(content=llm_result.content, source=self.id.type),
                        FunctionExecutionResultMessage(content=tool_call_results),
                    ]
                )
                llm_result = await self._model_client.create(
                    messages=[self._system_message] + message.context,
                    tools=self._tool_schema + self._delegate_tool_schema,
                    cancellation_token=ctx.cancellation_token,
                )
            else:
                return

        assert isinstance(llm_result.content, str)
        write_log(self.id.type, "User", llm_result.content, log_only=False)
        
        message.context.append(AssistantMessage(content=llm_result.content, source=self.id.type))
        await self.publish_message(
            AgentResponse(context=message.context, reply_to_topic_type=self._agent_topic_type),
            topic_id=TopicId(self._user_topic_type, source=self.id.key),
        )

class UserAgent(RoutedAgent):
    def __init__(self, description: str, user_topic_type: str, agent_topic_type: str) -> None:
        super().__init__(description)
        self._user_topic_type = user_topic_type
        self._agent_topic_type = agent_topic_type

    @message_handler
    async def handle_user_login(self, message: UserLogin, ctx: MessageContext) -> None:
        user_input = input("User: ")
        meta_str = f"User: {user_input}\nSession ID: {self.id.key}\nInitial Entry Point: {self._agent_topic_type}"
        write_log("", "", "", session_meta=meta_str)
        
        await self.publish_message(
            UserTask(context=[UserMessage(content=user_input, source="User")]),
            topic_id=TopicId(self._agent_topic_type, source=self.id.key),
        )

    @message_handler
    async def handle_task_result(self, message: AgentResponse, ctx: MessageContext) -> None:
        user_input = input("User (type 'exit' to close the session): ")
        if user_input.strip().lower() == "exit":
            write_log("User", "System", f"Session closed by user terminal signal.\n{'-'*80}")
            return
            
        write_log("User", message.reply_to_topic_type, user_input)
        message.context.append(UserMessage(content=user_input, source="User"))
        await self.publish_message(
            UserTask(context=message.context),
            topic_id=TopicId(message.reply_to_topic_type, source=self.id.key)
        )

# -- Tools --
def execute_order(product: str, price: int) -> str:
    return "Order Success"

def execute_refund(item_id: str) -> str:
    return "Refund success"

execute_order_tool = FunctionTool(execute_order, description="Execute order. Price in USD.")
execute_refund_tool = FunctionTool(execute_refund, description="Execute refund by item ID.")

# -- Topic Types & Delegate Tools --
sales_topic = "SalesAgent"
issues_topic = "IssuesAgent"
triage_topic = "TriageAgent"
user_topic = "User"

def transfer_to_sales() -> str:
    return sales_topic

def transfer_to_issues() -> str:
    return issues_topic

def transfer_back_to_triage() -> str:
    return triage_topic

transfer_to_sales_tool = FunctionTool(transfer_to_sales, description="Use for sales or buying related requests.")
transfer_to_issues_tool = FunctionTool(transfer_to_issues, description="Use for issues, repairs, or refunds.")
transfer_back_to_triage_tool = FunctionTool(transfer_back_to_triage, description="Call to go back to triage if request is outside your scope.")

async def main():
    runtime = SingleThreadedAgentRuntime()
    model_client = OpenAIChatCompletionClient(
        model=os.environ.get("MODEL_NAME", "gemini-2.0-flash"),
        api_key=os.environ.get("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model_info=ModelInfo(
            vision=True,
            function_calling=True,
            json_output=True,
            family="unknown",
            structured_output=True
        )
    )

    # Register Triage Agent
    triage_type = await AIAgent.register(
        runtime, type=triage_topic,
        factory=lambda: AIAgent(
            "Triage agent",
            SystemMessage(content="You are a triage agent for ACME Inc. Direct users to Sales or Issues department."),
            model_client,
            tools=[],
            delegate_tools=[transfer_to_issues_tool, transfer_to_sales_tool],
            agent_topic_type=triage_topic,
            user_topic_type=user_topic
        )
    )
    await runtime.add_subscription(TypeSubscription(topic_type=triage_topic, agent_type=triage_type.type))

    # Register Sales Agent
    sales_type = await AIAgent.register(
        runtime, type=sales_topic,
        factory=lambda: AIAgent(
            "Sales agent",
            SystemMessage(content="You are a sales agent. Help users buy items and execute their orders."),
            model_client,
            tools=[execute_order_tool],
            delegate_tools=[transfer_back_to_triage_tool],
            agent_topic_type=sales_topic,
            user_topic_type=user_topic
        )
    )
    await runtime.add_subscription(TypeSubscription(topic_type=sales_topic, agent_type=sales_type.type))

    # Register Issues Agent
    issues_type = await AIAgent.register(
        runtime, type=issues_topic,
        factory=lambda: AIAgent(
            "Issues agent",
            SystemMessage(content="You are an issues agent. Handle refunds and item problems."),
            model_client,
            tools=[execute_refund_tool],
            delegate_tools=[transfer_back_to_triage_tool],
            agent_topic_type=issues_topic,
            user_topic_type=user_topic
        )
    )
    await runtime.add_subscription(TypeSubscription(topic_type=issues_topic, agent_type=issues_type.type))

    # Register User Agent
    user_agent_type = await UserAgent.register(
        runtime, type=user_topic,
        factory=lambda: UserAgent("User agent", user_topic, triage_topic)
    )
    await runtime.add_subscription(TypeSubscription(topic_type=user_topic, agent_type=user_agent_type.type))

    runtime.start()
    session_id = str(uuid.uuid4())
    await runtime.publish_message(UserLogin(), topic_id=TopicId(user_topic, source=session_id))
    await runtime.stop_when_idle()
    await model_client.close()

if __name__ == "__main__":
    asyncio.run(main())