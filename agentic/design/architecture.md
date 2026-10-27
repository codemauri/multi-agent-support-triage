# UDA-Hub: Multi-Agent Architecture Design

## 1. Architecture Pattern

**Supervisor with Tool-Calling Routing**

A central Supervisor agent receives all incoming support tickets, classifies them based on content and metadata, and delegates to specialized agents via tool calls. Each specialized agent has access to its own tools for database operations and a shared knowledge base search tool. The Supervisor evaluates agent responses — including confidence in the knowledge match — and decides whether to resolve or escalate.

## 2. System Diagram

```mermaid
graph TD
    USER[Customer Ticket] --> SUPERVISOR[Supervisor Agent]
    MEMORY[(Memory Store<br/>SqliteSaver + Ticket History)] <-.-> SUPERVISOR

    SUPERVISOR -->|tool call| GENERAL[General Agent]
    SUPERVISOR -->|tool call| BILLING[Billing Agent]
    SUPERVISOR -->|tool call| TECHNICAL[Technical Agent]
    SUPERVISOR -->|tool call| ACCOUNT[Account Agent]
    SUPERVISOR -->|tool call| ESCALATION[Escalation Agent]

    GENERAL -->|response + confidence signal| SUPERVISOR
    BILLING -->|response + confidence signal| SUPERVISOR
    TECHNICAL -->|response + confidence signal| SUPERVISOR
    ACCOUNT -->|response + confidence signal| SUPERVISOR
    ESCALATION -->|response| SUPERVISOR

    SUPERVISOR -->|resolve| RESPONSE[Final Response to Customer]
    SUPERVISOR -->|escalate| HUMAN[Human Support Handoff]

    LOGGER[ToolLogger<br/>JSON per session] -.->|logs every tool call| SUPERVISOR

    subgraph MCP Tools — agentic/tools/
        CULTPASS_MCP["cultpass_server.py<br/>lookup_subscription | lookup_payment_history<br/>lookup_user | update_user_profile<br/>delete_account | check_account_status"]
        UDAHUB_MCP["udahub_server.py<br/>search_knowledge_base | lookup_ticket_history<br/>lookup_customer_history | create_escalation_ticket<br/>process_refund"]
    end

    GENERAL -.-> UDAHUB_MCP
    BILLING -.-> CULTPASS_MCP
    BILLING -.-> UDAHUB_MCP
    TECHNICAL -.-> UDAHUB_MCP
    ACCOUNT -.-> CULTPASS_MCP
    ACCOUNT -.-> UDAHUB_MCP
    ESCALATION -.-> UDAHUB_MCP
```

## 3. Agent Roles and Responsibilities

### Supervisor Agent
- **Role**: Central orchestrator and decision-maker
- **Responsibilities**:
  - Receives incoming tickets with content and metadata (channel, tags, urgency)
  - Queries long-term memory (`lookup_customer_history`) for the customer's previous interactions
  - Classifies ticket type based on content, metadata, and customer history
  - Routes to the appropriate specialized agent via tool call
  - Evaluates agent responses for completeness and confidence
  - Decides to resolve (return response to customer) or escalate (hand off to human)
  - Maintains conversation context across multi-turn interactions
- **Tools**: Route-to-agent tools (one per specialized agent), `lookup_customer_history`
- **Implementation**: Custom `StateGraph` — built from scratch, not prebuilt

### General Agent
- **Role**: General inquiry and FAQ specialist
- **Responsibilities**:
  - Handles questions that don't fit billing, technical, or account categories
  - Answers general FAQ questions (reservations, subscription overview, referrals, gift cards, accessibility)
  - Provides responses grounded in knowledge base articles
  - Evaluates whether KB results are relevant enough to resolve the ticket; if not, signals low confidence to the Supervisor
- **Tools**:
  - `search_knowledge_base` — search support articles; returns matched articles with titles, content, and tags

### Billing Agent
- **Role**: Billing and subscription operations specialist
- **Responsibilities**:
  - Handles refund requests, payment issues, and subscription inquiries
  - Looks up subscription status, plan type, and billing cycle
  - Checks payment history for duplicate charges or failed payments
  - Processes refund requests by updating ticket status and documenting the decision
  - Provides billing-related responses grounded in knowledge base articles
  - Evaluates whether KB results are relevant enough to resolve the ticket; if not, signals low confidence to the Supervisor
- **Tools**:
  - `lookup_subscription` — retrieve subscription details (plan, status, billing cycle)
  - `lookup_payment_history` — retrieve past payments and identify failed or duplicate charges
  - `process_refund` — verify eligibility, update ticket status to "refund_approved", and add a documentation message to the ticket
  - `search_knowledge_base` — search support articles for billing-related guidance

### Technical Agent
- **Role**: Technical issue resolution specialist
- **Responsibilities**:
  - Handles app crashes, QR code issues, login problems, and other technical issues
  - Looks up ticket history to check for recurring or known issues
  - Provides troubleshooting guidance based on knowledge base articles
  - Evaluates whether KB results are relevant enough to resolve the ticket; if not, signals low confidence to the Supervisor
- **Tools**:
  - `lookup_ticket_history` — check past tickets for recurring issues or known bugs
  - `search_knowledge_base` — search support articles for troubleshooting steps

### Account Agent
- **Role**: Account management specialist
- **Responsibilities**:
  - Handles profile updates, account deletion requests, and account access issues
  - Looks up user information and account details
  - Processes profile update requests (name, email, preferences)
  - Processes account deletion requests
  - Checks account status for blocked or locked accounts
  - Provides account-related guidance based on knowledge base articles
  - Evaluates whether KB results are relevant enough to resolve the ticket; if not, signals low confidence to the Supervisor
- **Tools**:
  - `lookup_user`             — retrieve user profile and account details
  - `update_user_profile`     — modify user profile fields (name, email, preferences)
  - `delete_account`          — process account deletion by updating account status
  - `check_account_status`    — check if an account is blocked, locked, or flagged
  - `search_knowledge_base`   — search support articles for account-related guidance

### Escalation Agent
- **Role**: Human support handoff specialist
- **Responsibilities**:
  - Activated when other agents cannot resolve the issue or confidence is low
  - Activated immediately for safety, discrimination, or accessibility emergencies
  - Creates an escalation ticket with full context (conversation history, attempted resolutions, reason for escalation)
  - Notifies the customer that their issue is being escalated
- **Tools**:
  - `create_escalation_ticket` — create an escalation record with full context for human support handoff

## 4. Information Flow and Decision-Making

### Step-by-Step Flow

1. **Ticket Intake**: Customer submits a support ticket with text content and metadata (channel, tags, status)
2. **Memory Lookup**: The Supervisor calls `lookup_customer_history` to check for the customer's previous interactions. If the customer is returning, their history (past tickets, prior resolutions, escalation history) is included in the context for routing and agent processing.
3. **Supervisor Classification**: The Supervisor LLM reads the ticket content, metadata, and customer history, then decides which specialized agent to route to by calling the corresponding tool. Routing considers:
   - **Content**: What the customer is asking about (billing, technical, account, general)
   - **Metadata tags**: Pre-assigned categories on the ticket
   - **Channel**: Where the ticket came from (email, chat, app)
   - **Urgency**: Safety or emergency signals trigger immediate escalation
   - **Customer history**: Returning customers with unresolved issues may be escalated
4. **Agent Processing**: The selected agent uses its MCP tools (DB lookups, knowledge base search) to gather information and formulate a response. The agent evaluates the relevance of retrieved KB articles and includes a confidence assessment in its response to the Supervisor.
5. **Response Evaluation**: The Supervisor receives the agent's response and evaluates:
   - If the response adequately addresses the issue with sufficient confidence → **Resolve** (return to customer)
   - If the agent signals low confidence or no relevant knowledge → **Escalate** (route to Escalation Agent)
   - If the issue is a safety or emergency concern → **Escalate immediately**
6. **Logging**: The `ToolLogger` records every tool invocation with structured JSON (timestamp, tool name, inputs, outputs). Checkpointer state history provides an additional searchable audit trail of the full ticket lifecycle.
7. **Memory Update**: The interaction is persisted automatically. Short-term memory (via `thread_id` + `SqliteSaver` checkpointer) maintains and persists the current session. Long-term context is available via `lookup_customer_history`, which queries the Ticket and TicketMessage tables for past resolved interactions.

### Decision Points

| Decision             | Input                                                                 | Outcome                                                                      |
|----------------------|-----------------------------------------------------------------------|------------------------------------------------------------------------------|
| Route to agent       | Ticket content + metadata (tags, channel, urgency) + customer history | Tool call to General, Billing, Technical, Account, or Escalation agent       |
| Resolve vs. escalate | Agent response + confidence assessment                                | Final response to customer or escalation handoff                             |
| Knowledge match      | Search query vs. knowledge base articles                              | Article content used in response, or "no relevant match" triggers escalation |
| Tool selection       | Agent's assessment of the ticket                                      | DB lookup, profile update, refund processing, or escalation                  |

## 5. Input and Output Handling

### Inputs
- **Ticket text**: Natural language description of the customer's issue
- **Ticket metadata**: Channel (email, chat, app), tags, status
- **User context**: Account ID, user ID, previous interaction history from memory
- **Knowledge base**: Support articles with titles, content, and tags

### Outputs
- **Resolution response**: Direct answer to the customer based on knowledge base articles and account data
- **Escalation**: Handoff to human support with full context summary (conversation history, attempted resolutions, escalation reason)
- **Database updates**: Refund processing, profile modifications, account status changes
- **Structured logs**: JSON log files with agent decisions, routing choices, tool usage, and outcomes at each step

### Example Scenarios

| Input                                  | Route            | Action                                       | Output                                          |
|----------------------------------------|------------------|----------------------------------------------|--------------------------------------------------|
| "How do I reserve an event?"           | General Agent    | Search KB for reservation article            | Reservation instructions from KB                 |
| "How do I refer a friend?"            | General Agent    | Search KB for referral article               | Referral program instructions from KB            |
| "I was charged twice this month"       | Billing Agent    | Lookup payment history + search KB           | Billing explanation or refund processing         |
| "What's included in my subscription?" | Billing Agent    | Lookup subscription + search KB              | Subscription details from KB                     |
| "The app keeps crashing on my phone"   | Technical Agent  | Check ticket history + search KB             | Troubleshooting steps or escalation              |
| "My QR code won't scan at the venue"   | Technical Agent  | Search KB for QR troubleshooting             | QR code troubleshooting steps                    |
| "I want to delete my account"          | Account Agent    | Lookup user + explain deletion from KB       | Account deletion instructions and processing     |
| "I need to update my email address"    | Account Agent    | Lookup user + update profile                 | Confirmation of profile update                   |
| "I feel unsafe at a venue"             | Escalation Agent | Create escalation ticket immediately         | Escalation confirmation to customer              |
| "I contacted you last week about this" | (any agent)      | Lookup customer history → route with context | Personalized response acknowledging prior issue  |

## 6. Memory Strategy

### State

The LangGraph state object (`AgentState`) maintains data during graph execution: conversation messages, ticket/customer identifiers, customer history from long-term memory, current agent assignment, confidence assessment, resolution status, and a log of tools used. Messages use the `add_messages` reducer for append semantics; `tools_used` uses list concatenation.

### Short-Term Memory (Session)

- **Mechanism**: `SqliteSaver` checkpointer from `langgraph.checkpoint.sqlite`
- **Scope**: Maintains conversation context within a session (multi-turn interactions)
- **Persistence**: Stored in SQLite (`data/core/memory.db`) — survives process restarts, unlike `MemorySaver`
- **Isolation**: Each session identified by `thread_id` in config
- **Inspectable**: `get_state()` retrieves current state; `get_state_history()` retrieves full execution history for debugging and auditing

### Long-Term Memory (Cross-Session)

- **Mechanism**: `lookup_customer_history` MCP tool that queries the existing Ticket, TicketMessage, and TicketMetadata tables by customer ID
- **Scope**: Retrieves past resolved tickets, prior resolutions, and escalation history across all sessions
- **Integration**: Supervisor calls this tool before routing to include customer context in the agent's input
- **Personalization**: Enables responses like "I see you contacted us about this before..." and avoids repeating troubleshooting steps already attempted

### Memory Flow

1. New ticket arrives → Supervisor calls `lookup_customer_history(customer_id)`
2. Customer history (if any) is added to context for the specialized agent
3. Agent processes ticket using tools and KB → returns response
4. `SqliteSaver` checkpointer automatically persists the full conversation state
5. On next session with same customer, step 1 retrieves this interaction's outcome

## 7. Knowledge Retrieval (RAG)

The `search_knowledge_base` MCP tool retrieves relevant support articles from the Knowledge table in `udahub.db` using keyword matching against article titles, content, and tags. Title matches are weighted higher than content matches. The tool returns the top-k matched articles with their title, content, and tags.

The agent's LLM uses the returned articles to formulate a grounded response. If no relevant articles are found, the agent signals low confidence to the Supervisor (see §8).

## 8. Confidence Scoring and Escalation

Confidence scoring determines when the system can resolve a ticket versus when it must escalate to a human. The agent's LLM evaluates whether retrieved KB articles adequately address the customer's issue — the same pattern used in agentic RAG (Course 2, L4).

### How It Works

1. The agent calls `search_knowledge_base` with the ticket content
2. The tool returns matched articles (titles, content, tags)
3. The agent's LLM evaluates the relevance of the results against the customer's specific issue
4. The agent includes a confidence assessment in its response to the Supervisor:
   - **High confidence**: relevant article found, resolution formulated
   - **Uncertain**: partially relevant article, flags uncertainty
   - **Low confidence**: no relevant articles or issue beyond KB coverage, signals escalation needed
5. The Supervisor makes the final resolve/escalate decision based on the agent's assessment

### Escalation Guidelines

| Agent Signal                           | Supervisor Action                                                    |
|----------------------------------------|----------------------------------------------------------------------|
| High confidence + complete resolution  | **Resolve** — return response to customer                            |
| Uncertain + partial resolution         | **Resolve with caveat** or **Escalate** based on issue severity      |
| Low confidence + no relevant KB match  | **Escalate** — route to Escalation Agent                             |
| Safety, discrimination, or emergency   | **Escalate immediately** — bypass normal flow                        |

## 9. Structured Logging

All tool activity is logged using a `ToolLogger` that writes structured JSON to a per-session log file.

### What Gets Logged

| Event                  | Details Captured                                           |
|------------------------|------------------------------------------------------------|
| Routing decisions      | Which agent was selected and why                           |
| Tool invocations       | Every tool call with inputs and outputs                    |
| Knowledge retrieval    | Articles matched and agent's confidence assessment         |
| Resolution/Escalation  | Final outcome and reasoning                                |

### Supplementary: Checkpointer State History

The `SqliteSaver` checkpointer provides a complete, inspectable execution history per thread — every state transition, message, and tool result — stored automatically. This gives a second layer of searchable audit data.

## 10. Error Handling

### Tool-Level

All MCP tools return structured error messages rather than raising exceptions. This allows the agent's LLM to reason about the error and take appropriate action (retry, use a different tool, or signal escalation).

### Edge Cases

| Scenario                                 | Behavior                                                                   |
|------------------------------------------|----------------------------------------------------------------------------|
| No KB articles match                     | Agent signals low confidence → Supervisor escalates                        |
| User not found in DB                     | Tool returns "not found" message → agent asks for clarification or escalates |
| Multiple issues in one ticket            | Supervisor routes to the most relevant agent; agent flags remaining issues  |
| Returning customer with unresolved issue | Customer history shows prior escalation → Supervisor may escalate directly |

## 11. Tool Architecture

### MCP Servers (Database & KB Tools) — `agentic/tools/`

All tools that abstract database access are implemented as **FastMCP servers**, following the instructor's recommendation. Each server runs as a stdio subprocess and exposes tools via the Model Context Protocol. Agents access them via `MultiServerMCPClient`.

| Server                | Database      | Tools                                                                                                          |
|-----------------------|---------------|----------------------------------------------------------------------------------------------------------------|
| `cultpass_server.py`  | `cultpass.db` | `lookup_subscription`, `lookup_payment_history`, `lookup_user`, `update_user_profile`, `delete_account`, `check_account_status` |
| `udahub_server.py`    | `udahub.db`   | `search_knowledge_base`, `lookup_ticket_history`, `lookup_customer_history`, `create_escalation_ticket`, `process_refund`       |

### Routing Tools — `agentic/workflow.py`

Agent routing tools (`route_to_billing`, `route_to_technical`, etc.) are `@tool`-decorated functions in `workflow.py`. They are internal orchestration — they invoke agents, not databases — and do not belong in MCP servers.

### Why This Split

- **MCP for DB tools**: Modularity, clear separation between data access and orchestration, aligns with Course 1 FastMCP patterns and instructor guidance
- **@tool for routing**: Routing tools invoke agents internally; wrapping them in MCP subprocesses would add complexity with no benefit

## 12. Technology Stack

| Component              | Technology                                                                           |
|------------------------|--------------------------------------------------------------------------------------|
| Orchestration framework | LangGraph — `StateGraph` for supervisor, `create_react_agent` for specialized agents |
| LLM                    | OpenAI GPT-4o-mini via Vocareum endpoint                                             |
| Databases              | SQLite via SQLAlchemy — `cultpass.db` (external), `udahub.db` (core)                 |
| Short-term memory      | `SqliteSaver` checkpointer with `thread_id` isolation                                |
| Long-term memory       | `lookup_customer_history` MCP tool querying Ticket/TicketMessage tables               |
| Knowledge retrieval    | `search_knowledge_base` MCP tool with keyword matching against Knowledge table        |
| DB/KB tools            | FastMCP servers in `agentic/tools/`, loaded via `MultiServerMCPClient`               |
| Routing tools          | `@tool` decorators in `workflow.py`                                                  |
| Logging                | `ToolLogger` (JSON per session) + checkpointer `get_state_history()`                 |
| Confidence             | LLM-based evaluation — agent judges KB relevance, Supervisor decides resolve/escalate |
