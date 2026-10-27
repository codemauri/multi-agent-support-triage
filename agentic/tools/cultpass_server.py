"""
FastMCP server exposing CultPass database tools.

Provides user, subscription, and account management tools
that query/modify cultpass.db via SQLAlchemy.

Run: python -m agentic.tools.cultpass_server (from solution/ dir)
"""

import os
import sys
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add solution/ to path so we can import data.models
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from data.models.cultpass import Base, User, Subscription, Reservation, Experience

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "external", "cultpass.db")
engine = create_engine(f"sqlite:///{os.path.abspath(DB_PATH)}", echo=False)
SessionLocal = sessionmaker(bind=engine)

mcp = FastMCP("cultpass")


@mcp.tool()
def lookup_user(user_id: str) -> dict:
    """Look up a CultPass user's profile by their user ID.

    Returns the user's name, email, account status, and creation date.
    Use this when you need to verify a customer's identity or check their profile.
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            return {"error": f"No user found with ID: {user_id}"}
        return {
            "user_id": user.user_id,
            "full_name": user.full_name,
            "email": user.email,
            "is_blocked": user.is_blocked,
            "created_at": str(user.created_at),
        }
    except Exception as e:
        return {"error": f"Failed to look up user: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def lookup_subscription(user_id: str) -> dict:
    """Look up a user's subscription details by their user ID.

    Returns plan tier, status, monthly quota, and billing dates.
    Use this for billing inquiries or to check what plan a customer is on.
    """
    session = SessionLocal()
    try:
        sub = session.query(Subscription).filter_by(user_id=user_id).first()
        if not sub:
            return {"error": f"No subscription found for user: {user_id}"}
        return {
            "subscription_id": sub.subscription_id,
            "user_id": sub.user_id,
            "status": sub.status,
            "tier": sub.tier,
            "monthly_quota": sub.monthly_quota,
            "started_at": str(sub.started_at),
            "ended_at": str(sub.ended_at) if sub.ended_at else None,
        }
    except Exception as e:
        return {"error": f"Failed to look up subscription: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def lookup_payment_history(user_id: str) -> dict:
    """Look up a user's payment and billing history.

    Returns subscription billing details including plan changes, status,
    and billing period. Use this when a customer reports duplicate charges
    or payment issues.
    """
    session = SessionLocal()
    try:
        sub = session.query(Subscription).filter_by(user_id=user_id).first()
        if not sub:
            return {"error": f"No billing history found for user: {user_id}"}
        return {
            "user_id": sub.user_id,
            "subscription_status": sub.status,
            "tier": sub.tier,
            "monthly_quota": sub.monthly_quota,
            "billing_start": str(sub.started_at),
            "billing_end": str(sub.ended_at) if sub.ended_at else "ongoing",
            "last_updated": str(sub.updated_at),
        }
    except Exception as e:
        return {"error": f"Failed to look up payment history: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def update_user_profile(user_id: str, full_name: str = None, email: str = None) -> dict:
    """Update a CultPass user's profile fields.

    Can update the user's name and/or email. At least one field must be provided.
    Use this when a customer requests to change their profile information.
    """
    if not full_name and not email:
        return {"error": "At least one of full_name or email must be provided"}
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            return {"error": f"No user found with ID: {user_id}"}
        updated_fields = []
        if full_name:
            user.full_name = full_name
            updated_fields.append(f"full_name='{full_name}'")
        if email:
            user.email = email
            updated_fields.append(f"email='{email}'")
        user.updated_at = datetime.now()
        session.commit()
        return {
            "status": "updated",
            "user_id": user_id,
            "updated_fields": updated_fields,
        }
    except Exception as e:
        session.rollback()
        return {"error": f"Failed to update profile: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def delete_account(user_id: str) -> dict:
    """Mark a CultPass user account as blocked/deleted.

    This does not permanently delete the account — it sets is_blocked=True
    and cancels any active subscription. Use this when a customer requests
    account deletion.
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            return {"error": f"No user found with ID: {user_id}"}
        if user.is_blocked:
            return {"status": "already_blocked", "user_id": user_id}
        user.is_blocked = True
        user.updated_at = datetime.now()
        sub = session.query(Subscription).filter_by(user_id=user_id).first()
        if sub and sub.status == "active":
            sub.status = "cancelled"
            sub.ended_at = datetime.now()
            sub.updated_at = datetime.now()
        session.commit()
        return {
            "status": "account_blocked",
            "user_id": user_id,
            "subscription_cancelled": sub is not None and sub.status == "cancelled",
        }
    except Exception as e:
        session.rollback()
        return {"error": f"Failed to delete account: {str(e)}"}
    finally:
        session.close()


@mcp.tool()
def check_account_status(user_id: str) -> dict:
    """Check if a CultPass user account is active, blocked, or has issues.

    Returns the account status including blocked state and subscription status.
    Use this when a customer reports they can't access their account.
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            return {"error": f"No user found with ID: {user_id}"}
        sub = session.query(Subscription).filter_by(user_id=user_id).first()
        return {
            "user_id": user.user_id,
            "full_name": user.full_name,
            "is_blocked": user.is_blocked,
            "account_status": "blocked" if user.is_blocked else "active",
            "subscription_status": sub.status if sub else "none",
            "subscription_tier": sub.tier if sub else None,
        }
    except Exception as e:
        return {"error": f"Failed to check account status: {str(e)}"}
    finally:
        session.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
