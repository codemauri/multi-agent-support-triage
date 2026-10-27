# Multi-Agent Support Triage (UDA-Hub)

A LangGraph multi-agent system that **reads, classifies, routes, and resolves customer support tickets**,
and **escalates to a human** when it can't answer confidently or the issue involves safety.

Built for CultPass, a fictional cultural-experiences subscription service. The design target is a
"Universal Decision Agent" that plugs into support platforms through MCP tools; this implementation
integrates one platform (CultPass).

> Built as the capstone project of Udacity's *Agentic AI Engineer with LangChain and LangGraph*
> nanodegree (Course 3, Advanced Agentic AI Techniques).

## Design highlights

- **Supervisor routing by urgency and complexity.** A custom `StateGraph` supervisor classifies each ticket
  (urgency: low/medium/high; complexity: simple/moderate/complex) and hands it to one of five specialized
  agents through tool calls.
- **Deterministic confidence, not LLM self-assessment.** Answer confidence is computed from knowledge-base
  relevance scores (`HIGH` ≥ 3.0, `MEDIUM` 1.0–3.0, `LOW` < 1.0 or no KB search at all) and drives the
  resolve-or-escalate decision.
- **Safety-first escalation.** Safety, discrimination, and accessibility emergencies go straight to the
  escalation agent, which opens a ticket for a human specialist.
- **Tools behind MCP servers.** Database and knowledge-base operations are exposed by two FastMCP servers
  (stdio transport), which keeps agent logic separate from data access.
- **Two kinds of memory.** Short-term: an `AsyncSqliteSaver` checkpointer keeps conversation state per
  `thread_id`. Long-term: customer interaction history is recalled through an MCP tool.
- **Observability.** Every tool invocation is written as structured JSON, one log file per session (`logs/`).

## Architecture

```
ticket ──► Supervisor ──(route_to_* tools)──► General │ Billing │ Technical │ Account │ Escalation
               ▲                                         │
               └──────────── tool results ◄──────────────┘
           Supervisor ──► format_response (adds confidence tag) ──► reply
```

| Agent | Handles |
|---|---|
| General | FAQ, reservations, gift cards |
| Billing | payments, refunds, subscriptions |
| Technical | app crashes, QR codes, login bugs |
| Account | profile updates, deletion, access issues |
| Escalation | handoff to human support when automated resolution fails |

**MCP tools**

| Server | Tools |
|---|---|
| CultPass (`agentic/tools/cultpass_server.py`) | `lookup_user`, `lookup_subscription`, `lookup_payment_history`, `update_user_profile`, `delete_account`, `check_account_status` |
| UdaHub (`agentic/tools/udahub_server.py`) | `search_knowledge_base`, `lookup_ticket_history`, `lookup_customer_history`, `create_escalation_ticket`, `process_refund` |

The full design rationale (decision points, memory strategy, error handling) is in
[`agentic/design/architecture.md`](agentic/design/architecture.md).

## Tests

`03_agentic_app.ipynb` runs 5 end-to-end scenarios:

| Test | Type | Expected outcome |
|---|---|---|
| 1. Billing refund inquiry | Resolution | Confidence: HIGH |
| 2. General FAQ (gift card) | Resolution | Confidence: HIGH |
| 3. Technical (app crash) | Resolution | Confidence: HIGH |
| 4. No KB match (subscription transfer) | Escalation | Confidence: LOW |
| 5. Safety/discrimination | Escalation | Escalated to a human specialist |

Verification cells confirm the structured logging and state persistence. Sample session logs are in `logs/`.

## Running it

Requires Python 3.13.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/python -m ipykernel install --user --name=project --display-name="Python (Project)"
```

Create a `.env` file:

```
VOCAREUM_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here
```

The LLM (`gpt-4o-mini`) is called through Udacity's Vocareum OpenAI proxy (`base_url` in
`agentic/workflow.py`). To use OpenAI directly, change `base_url` and `api_key` there.

Run the notebooks in order:

1. `01_external_db_setup.ipynb`: seeds the CultPass database (`data/external/cultpass.db`)
2. `02_core_db_setup.ipynb`: seeds the UdaHub database (`data/core/udahub.db`), loads 17 knowledge-base
   articles, and registers the CultPass users
3. `03_agentic_app.ipynb`: runs the orchestrator against the test scenarios

## Built with

[LangGraph](https://github.com/langchain-ai/langgraph) · [LangChain](https://github.com/langchain-ai/langchain) ·
[FastMCP](https://github.com/jlowin/fastmcp) · [SQLAlchemy](https://www.sqlalchemy.org/) · OpenAI `gpt-4o-mini`
