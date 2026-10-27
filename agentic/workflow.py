"""
UDA-Hub Orchestrator — Custom StateGraph supervisor for CultPass support.

The supervisor LLM classifies each customer message and routes it to the right
specialized agent via @tool routing functions. MCP servers provide database access.
"""

import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Annotated
from typing_extensions import TypedDict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, InjectedState
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_mcp_adapters.client import MultiServerMCPClient

from agentic.agents.general import create_general_agent
from agentic.agents.billing import create_billing_agent
from agentic.agents.technical import create_technical_agent
from agentic.agents.account import create_account_agent
from agentic.agents.escalation import create_escalation_agent

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # solution/
TOOLS_DIR = BASE_DIR / "agentic" / "tools"
MEMORY_DB = BASE_DIR / "data" / "core" / "memory.db"
LOGS_DIR = BASE_DIR / "logs"
PYTHON = str(BASE_DIR / ".venv" / "bin" / "python")        # venv Python for MCP subprocesses

# ---------------------------------------------------------------------------
# 1. AgentState — the shared state flowing through every graph node
# ---------------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]   # conversation history (append-only)
    ticket_id: str                                          # current support ticket
    customer_id: str                                        # customer identifier
    current_agent: Optional[str]                            # which agent handled last
    resolution_status: Optional[str]                        # "resolved" | "escalated" | None


# ---------------------------------------------------------------------------
# 2. ToolLogger — structured JSON logging of every tool invocation
# ---------------------------------------------------------------------------
class ToolLogger:
    """Logs every tool call to a per-session JSON file for observability."""

    def __init__(self, session_id: str):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = LOGS_DIR / f"session_{session_id}.json"
        self.entries: list[dict] = []

    def log_tool_use(self, tool_name: str, inputs: dict, output: str):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "input": inputs,
            "output": output[:500],  # truncate large outputs
        }
        self.entries.append(entry)
        # Auto-save after every call so nothing is lost on crash
        with open(self.log_file, "w") as f:
            json.dump(self.entries, f, indent=2)


# ---------------------------------------------------------------------------
# 3. Supervisor system prompt
# ---------------------------------------------------------------------------
SUPERVISOR_PROMPT = SystemMessage(content="""\
You are the Supervisor Agent for UDA-Hub, the customer support system for
CultPass — a cultural experiences subscription service.

Your job is to classify each customer message and route it to the right
specialized agent using the routing tools available to you.

Before routing, you MUST assess:
- **urgency**: "low" (general questions), "medium" (service issues), or
  "high" (account security, discrimination, safety, repeated failures)
- **complexity**: "simple" (single question, clear category), "moderate"
  (multiple concerns or needs investigation), or "complex" (cross-domain,
  escalation history, or ambiguous)

Routing guidelines:
- Billing issues (payments, refunds, subscriptions) → route_to_billing
- Technical issues (app crashes, QR codes, login bugs) → route_to_technical
- Account issues (profile updates, deletion, access) → route_to_account
- General inquiries (FAQ, reservations, gift cards) → route_to_general
- If an agent signals it cannot resolve the issue, or if a safety/
  discrimination/accessibility emergency is detected → route_to_escalation
- High urgency issues should be routed to escalation if they involve safety,
  discrimination, or repeated unresolved failures.

Rules:
1. Always route to exactly ONE agent per turn.
2. Never answer the customer directly — delegate to the appropriate agent.
3. If the agent's response signals it needs escalation, route to escalation.
4. Pass the assessed urgency and complexity to the routing tool.
5. After receiving the agent's response, relay it EXACTLY to the customer.
   Do NOT summarize, rephrase, or add your own commentary. The agent's
   response IS the final answer.
6. Be concise in your reasoning.
""")


# ---------------------------------------------------------------------------
# 4. Build the orchestrator (async — MCP servers need async startup)
# ---------------------------------------------------------------------------
async def build_orchestrator():
    """
    Creates the full supervisor graph with MCP tools, specialized agents,
    routing tools, and SqliteSaver checkpointing.
    """

    # -- LLM ----------------------------------------------------------------
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        base_url="https://openai.vocareum.com/v1",
        api_key=os.getenv("VOCAREUM_API_KEY"),
    )

    # -- MCP client: launch both tool servers --------------------------------
    mcp_client = MultiServerMCPClient(
        {
            "cultpass": {
                "command": PYTHON,
                "args": [str(TOOLS_DIR / "cultpass_server.py")],
                "transport": "stdio",
            },
            "udahub": {
                "command": PYTHON,
                "args": [str(TOOLS_DIR / "udahub_server.py")],
                "transport": "stdio",
            },
        }
    )
    mcp_tools = await mcp_client.get_tools()

    # -- Per-session logger --------------------------------------------------
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    logger = ToolLogger(session_id)

    # -- Specialized agents --------------------------------------------------
    general_agent = create_general_agent(llm, mcp_tools)
    billing_agent = create_billing_agent(llm, mcp_tools)
    technical_agent = create_technical_agent(llm, mcp_tools)
    account_agent = create_account_agent(llm, mcp_tools)
    escalation_agent = create_escalation_agent(llm, mcp_tools)

    # -- Helper: look up customer history for long-term memory -----------------
    history_tool = next((t for t in mcp_tools if t.name == "lookup_customer_history"), None)

    async def _build_agent_message(ticket_content: str, customer_id: str) -> str:
        """Prepend customer history to the ticket content for personalization."""
        if history_tool and customer_id:
            try:
                history = await history_tool.ainvoke({"external_user_id": customer_id})
                logger.log_tool_use("lookup_customer_history", {"customer_id": customer_id}, str(history)[:300])
                return f"Customer history:\n{history}\n\nCurrent issue:\n{ticket_content}"
            except Exception:
                pass
        return ticket_content

    # -- Helper: compute confidence from KB relevance scores -------------------
    def _compute_confidence(agent_result: dict) -> str:
        """Extract the highest relevance_score from search_knowledge_base tool
        calls in the agent's message history and map to HIGH/MEDIUM/LOW."""
        import re
        max_score = 0.0
        found_kb_call = False

        for msg in agent_result.get("messages", []):
            # ToolMessages from search_knowledge_base contain the results
            if hasattr(msg, "name") and msg.name == "search_knowledge_base":
                found_kb_call = True
                # msg.content may be a string or a list — normalize to string
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                # Extract relevance_score values from the response string
                scores = re.findall(r"'relevance_score':\s*([\d.]+)", content)
                if not scores:
                    scores = re.findall(r'"relevance_score":\s*([\d.]+)', content)
                for s in scores:
                    max_score = max(max_score, float(s))

        if not found_kb_call or max_score < 1.0:
            return "LOW"
        elif max_score < 3.0:
            return "MEDIUM"
        else:
            return "HIGH"

    # -- Routing tools (async — MCP tools require async invocation) -----------
    @tool
    async def route_to_general(
        ticket_content: str, urgency: str, complexity: str,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Route to General Agent for FAQ, reservations, gift cards, and general inquiries.
        urgency: low, medium, or high. complexity: simple, moderate, or complex."""
        customer_id = state.get("customer_id", "")
        logger.log_tool_use("route_to_general", {"ticket_content": ticket_content, "urgency": urgency, "complexity": complexity, "customer_id": customer_id}, "invoking general agent")
        context = await _build_agent_message(ticket_content, customer_id)
        result = await general_agent.ainvoke({"messages": [HumanMessage(content=context)]})
        response = result["messages"][-1].content
        confidence = _compute_confidence(result)
        response += f"\n\n[Confidence: {confidence}]"
        logger.log_tool_use("general_agent_response", {"confidence": confidence}, response)
        return response

    @tool
    async def route_to_billing(
        ticket_content: str, urgency: str, complexity: str,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Route to Billing Agent for payment issues, refunds, and subscription inquiries.
        urgency: low, medium, or high. complexity: simple, moderate, or complex."""
        customer_id = state.get("customer_id", "")
        logger.log_tool_use("route_to_billing", {"ticket_content": ticket_content, "urgency": urgency, "complexity": complexity, "customer_id": customer_id}, "invoking billing agent")
        context = await _build_agent_message(ticket_content, customer_id)
        result = await billing_agent.ainvoke({"messages": [HumanMessage(content=context)]})
        response = result["messages"][-1].content
        confidence = _compute_confidence(result)
        response += f"\n\n[Confidence: {confidence}]"
        logger.log_tool_use("billing_agent_response", {"confidence": confidence}, response)
        return response

    @tool
    async def route_to_technical(
        ticket_content: str, urgency: str, complexity: str,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Route to Technical Agent for app crashes, QR code issues, and login problems.
        urgency: low, medium, or high. complexity: simple, moderate, or complex."""
        customer_id = state.get("customer_id", "")
        logger.log_tool_use("route_to_technical", {"ticket_content": ticket_content, "urgency": urgency, "complexity": complexity, "customer_id": customer_id}, "invoking technical agent")
        context = await _build_agent_message(ticket_content, customer_id)
        result = await technical_agent.ainvoke({"messages": [HumanMessage(content=context)]})
        response = result["messages"][-1].content
        confidence = _compute_confidence(result)
        response += f"\n\n[Confidence: {confidence}]"
        logger.log_tool_use("technical_agent_response", {"confidence": confidence}, response)
        return response

    @tool
    async def route_to_account(
        ticket_content: str, urgency: str, complexity: str,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Route to Account Agent for profile updates, account deletion, and access issues.
        urgency: low, medium, or high. complexity: simple, moderate, or complex."""
        customer_id = state.get("customer_id", "")
        logger.log_tool_use("route_to_account", {"ticket_content": ticket_content, "urgency": urgency, "complexity": complexity, "customer_id": customer_id}, "invoking account agent")
        context = await _build_agent_message(ticket_content, customer_id)
        result = await account_agent.ainvoke({"messages": [HumanMessage(content=context)]})
        response = result["messages"][-1].content
        confidence = _compute_confidence(result)
        response += f"\n\n[Confidence: {confidence}]"
        logger.log_tool_use("account_agent_response", {"confidence": confidence}, response)
        return response

    @tool
    async def route_to_escalation(
        ticket_content: str, urgency: str, complexity: str,
        state: Annotated[dict, InjectedState],
    ) -> str:
        """Route to Escalation Agent when other agents cannot resolve, or for safety/discrimination emergencies.
        urgency: low, medium, or high. complexity: simple, moderate, or complex."""
        customer_id = state.get("customer_id", "")
        ticket_id = state.get("ticket_id", "unknown")
        logger.log_tool_use("route_to_escalation", {"ticket_content": ticket_content, "urgency": urgency, "complexity": complexity, "customer_id": customer_id}, "invoking escalation agent")
        context = await _build_agent_message(ticket_content, customer_id)
        context += f"\n\nTicket ID for escalation: {ticket_id}\nCustomer ID: {customer_id}"
        result = await escalation_agent.ainvoke({"messages": [HumanMessage(content=context)]})
        response = result["messages"][-1].content
        logger.log_tool_use("escalation_agent_response", {}, response)
        return response

    routing_tools = [
        route_to_general,
        route_to_billing,
        route_to_technical,
        route_to_account,
        route_to_escalation,
    ]

    # -- Bind routing tools to the supervisor LLM ----------------------------
    supervisor_llm = llm.bind_tools(routing_tools)

    # -- Graph nodes ----------------------------------------------------------
    def supervisor_node(state: AgentState) -> dict:
        """The supervisor decides which agent to route to."""
        response = supervisor_llm.invoke(
            [SUPERVISOR_PROMPT] + state["messages"]
        )
        return {"messages": [response]}

    # ToolNode executes whichever routing tool the supervisor called
    tool_node = ToolNode(routing_tools)

    # -- Conditional edge: continue routing or finish? -----------------------
    def should_continue(state: AgentState) -> str:
        """Check if the supervisor made a tool call or is done."""
        last_message = state["messages"][-1]
        # If the LLM returned tool_calls, execute them
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        # Otherwise the conversation turn is complete
        return "format_response"

    # -- Post-processing: ensure confidence tag is in the final response -----
    def format_response(state: AgentState) -> dict:
        """Ensure the final customer response includes the confidence tag
        from the routing tool, even if the supervisor stripped it."""
        import re
        last_msg = state["messages"][-1]
        # Find the last ToolMessage with a confidence tag
        confidence_tag = None
        for msg in reversed(state["messages"]):
            name = getattr(msg, "name", None)
            if name and name.startswith("route_to_"):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                match = re.search(r"\[Confidence:\s*(HIGH|MEDIUM|LOW)\]", content)
                if match:
                    confidence_tag = match.group(0)
                break
        # If the supervisor's response doesn't have the tag, append it
        if confidence_tag and confidence_tag not in last_msg.content:
            from langchain_core.messages import AIMessage as AI
            return {"messages": [AI(content=last_msg.content + f"\n\n{confidence_tag}")]}
        return {"messages": [last_msg]}

    # -- Build the StateGraph ------------------------------------------------
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("tools", tool_node)
    workflow.add_node("format_response", format_response)

    # Set entry point
    workflow.set_entry_point("supervisor")

    # Add edges
    workflow.add_conditional_edges("supervisor", should_continue, {"tools": "tools", "format_response": "format_response"})
    workflow.add_edge("tools", "supervisor")  # after tool execution, go back to supervisor
    workflow.add_edge("format_response", END)

    # -- Compile with AsyncSqliteSaver checkpointer ----------------------------
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(MEMORY_DB))
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()

    orchestrator = workflow.compile(checkpointer=checkpointer)

    return orchestrator


# ---------------------------------------------------------------------------
# 5. Synchronous wrapper for notebook/script usage
# ---------------------------------------------------------------------------
def create_orchestrator():
    """
    Synchronous entry point — call this from notebooks or scripts.
    Returns the compiled orchestrator graph.
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Inside Jupyter — use nest_asyncio to allow nested async calls
        import nest_asyncio
        nest_asyncio.apply()
    return asyncio.get_event_loop().run_until_complete(build_orchestrator())
