"""
FastMCP server exposing UdaHub database tools.

Provides knowledge base search, ticket history, customer history,
escalation, and refund tools that query/modify udahub.db via SQLAlchemy.

Run: python -m agentic.tools.udahub_server (from solution/ dir)
"""

import os
import sys
import uuid
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add solution/ to path so we can import data.models
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from data.models.udahub import Base, Account, User, Ticket, TicketMetadata, TicketMessage, Knowledge, RoleEnum

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "core", "udahub.db")
engine = create_engine(f"sqlite:///{os.path.abspath(DB_PATH)}", echo=False)
SessionLocal = sessionmaker(bind=engine)

mcp = FastMCP("udahub")


@mcp.tool()
def search_knowledge_base(query: str, max_results: int = 3) -> dict:
    """Search the support knowledge base for articles relevant to a customer's issue.

    Performs keyword matching against article titles, content, and tags.
    Title matches are weighted higher than content matches.
    Returns the top matching articles with their full content.
    """
    session = SessionLocal()
    try:
        articles = session.query(Knowledge).all()
        if not articles:
            return {"results": [], "message": "Knowledge base is empty"}

        keywords = query.lower().split()
        scored = []
        for article in articles:
            score = 0.0
            title_lower = article.title.lower()
            content_lower = article.content.lower()
            tags_lower = (article.tags or "").lower()

            for kw in keywords:
                if kw in title_lower:
                    score += 2.0
                score += content_lower.count(kw) * 0.5
                if kw in tags_lower:
                    score += 1.0

            if score > 0:
                scored.append({
                    "article_id": article.article_id,
                    "title": article.title,
                    "content": article.content,
                    "tags": article.tags,
                    "relevance_score": round(score, 1),
                })

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        results = scored[:max_results]

        if not results:
            return {"results": [], "message": f"No articles found matching: {query}"}

        return {"results": results, "total_matches": len(scored)}
    except Exception as e:
        return {"error": f"Failed to search knowledge base: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def lookup_ticket_history(user_id: str) -> dict:
    """Look up past support tickets for a user by their UdaHub user ID.

    Returns all tickets with their metadata and messages.
    Use this to check for recurring issues or prior resolutions.
    Note: user_id here is the UdaHub internal user ID, not the CultPass user ID.
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            return {"error": f"No user found with ID: {user_id}"}

        tickets = session.query(Ticket).filter_by(user_id=user_id).all()
        if not tickets:
            return {"user_id": user_id, "tickets": [], "message": "No ticket history found"}

        ticket_list = []
        for ticket in tickets:
            meta = ticket.ticket_metadata
            messages = [
                {
                    "role": msg.role.value,
                    "content": msg.content,
                    "created_at": str(msg.created_at),
                }
                for msg in ticket.messages
            ]
            ticket_list.append({
                "ticket_id": ticket.ticket_id,
                "channel": ticket.channel,
                "created_at": str(ticket.created_at),
                "status": meta.status if meta else "unknown",
                "issue_type": meta.main_issue_type if meta else None,
                "tags": meta.tags if meta else None,
                "messages": messages,
            })

        return {"user_id": user_id, "ticket_count": len(ticket_list), "tickets": ticket_list}
    except Exception as e:
        return {"error": f"Failed to look up ticket history: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def lookup_customer_history(external_user_id: str) -> dict:
    """Look up a customer's full support history using their CultPass user ID.

    Bridges the CultPass user ID to UdaHub records and retrieves all past
    tickets, resolutions, and interactions. Use this to personalize responses
    for returning customers.
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(external_user_id=external_user_id).first()
        if not user:
            return {"external_user_id": external_user_id, "tickets": [], "message": "No history found for this customer"}

        tickets = session.query(Ticket).filter_by(user_id=user.user_id).all()
        if not tickets:
            return {
                "external_user_id": external_user_id,
                "udahub_user_id": user.user_id,
                "customer_name": user.user_name,
                "tickets": [],
                "message": "Customer exists but has no ticket history",
            }

        summaries = []
        for ticket in tickets:
            meta = ticket.ticket_metadata
            last_msg = ticket.messages[-1] if ticket.messages else None
            summaries.append({
                "ticket_id": ticket.ticket_id,
                "channel": ticket.channel,
                "created_at": str(ticket.created_at),
                "status": meta.status if meta else "unknown",
                "issue_type": meta.main_issue_type if meta else None,
                "tags": meta.tags if meta else None,
                "message_count": len(ticket.messages),
                "last_message": last_msg.content[:200] if last_msg else None,
            })

        return {
            "external_user_id": external_user_id,
            "udahub_user_id": user.user_id,
            "customer_name": user.user_name,
            "total_tickets": len(summaries),
            "tickets": summaries,
        }
    except Exception as e:
        return {"error": f"Failed to look up customer history: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def create_escalation_ticket(ticket_id: str, reason: str, context: str, customer_id: str = "") -> dict:
    """Create an escalation record for a ticket that requires human support.

    Updates the ticket status to 'escalated' and adds an escalation message
    with the reason and context. Use this when the issue cannot be resolved
    by the automated system. Pass customer_id (external user ID) to create
    a new ticket if one doesn't exist yet.
    """
    session = SessionLocal()
    try:
        ticket = session.query(Ticket).filter_by(ticket_id=ticket_id).first()
        if not ticket and customer_id:
            # Look up the UdaHub user by their external (CultPass) ID
            user = session.query(User).filter_by(external_user_id=customer_id).first()
            if user:
                ticket = Ticket(
                    ticket_id=ticket_id,
                    account_id=user.account_id,
                    user_id=user.user_id,
                    channel="chat",
                    created_at=datetime.now(),
                )
                session.add(ticket)
                meta = TicketMetadata(
                    ticket_id=ticket_id,
                    status="escalated",
                    main_issue_type="escalation",
                    updated_at=datetime.now(),
                )
                session.add(meta)
                session.flush()

        if ticket:
            meta = ticket.ticket_metadata
            if meta:
                meta.status = "escalated"
                meta.updated_at = datetime.now()

            escalation_msg = TicketMessage(
                message_id=str(uuid.uuid4()),
                ticket_id=ticket_id,
                role=RoleEnum.system,
                content=f"ESCALATED: {reason}\n\nContext: {context}",
                created_at=datetime.now(),
            )
            session.add(escalation_msg)
            session.commit()

            return {
                "status": "escalated",
                "ticket_id": ticket_id,
                "reason": reason,
                "message_id": escalation_msg.message_id,
            }

        return {
            "status": "escalated",
            "ticket_id": ticket_id,
            "reason": reason,
            "note": "No matching user found — escalation logged without DB record",
        }
    except Exception as e:
        session.rollback()
        return {"error": f"Failed to create escalation: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def process_refund(ticket_id: str, reason: str) -> dict:
    """Process a refund request for a support ticket.

    Updates the ticket status to 'refund_approved' and adds a documentation
    message. Use this when a refund has been verified as eligible.
    """
    session = SessionLocal()
    try:
        ticket = session.query(Ticket).filter_by(ticket_id=ticket_id).first()
        if not ticket:
            return {"error": f"No ticket found with ID: {ticket_id}"}

        meta = ticket.ticket_metadata
        if meta:
            meta.status = "refund_approved"
            meta.updated_at = datetime.now()

        refund_msg = TicketMessage(
            message_id=str(uuid.uuid4()),
            ticket_id=ticket_id,
            role=RoleEnum.system,
            content=f"REFUND APPROVED: {reason}",
            created_at=datetime.now(),
        )
        session.add(refund_msg)
        session.commit()

        return {
            "status": "refund_approved",
            "ticket_id": ticket_id,
            "reason": reason,
            "message_id": refund_msg.message_id,
        }
    except Exception as e:
        session.rollback()
        return {"error": f"Failed to process refund: {str(e)}"}
    finally:
        session.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
