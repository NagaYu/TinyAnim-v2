"""
TinyAnim — Stripe subscription billing
======================================

Encapsulates all Stripe interaction:

* ``create_checkout_session`` — start a Pro subscription (Stripe Checkout).
* ``create_portal_session``   — let a customer manage/cancel their plan.
* ``handle_webhook``          — reconcile subscription state from Stripe events.

Runs entirely in Stripe **test mode** until live keys are supplied via env.
If billing is not configured the calling routes degrade gracefully.
"""

from __future__ import annotations

import logging

import stripe
from sqlalchemy.orm import Session

from .config import settings
from .models import User
from .plans import PLAN_FREE, PLAN_PRO

log = logging.getLogger("tinyanim.billing")

if settings.STRIPE_SECRET_KEY:
    stripe.api_key = settings.STRIPE_SECRET_KEY

# Subscription statuses that count as "actively paying".
_ACTIVE_STATUSES = {"active", "trialing"}


def _ensure_configured() -> None:
    if not settings.billing_enabled:
        raise RuntimeError("Billing is not configured (missing Stripe keys / price id).")


def _ensure_customer(session: Session, user: User) -> str:
    """Return the user's Stripe customer id, creating one if needed."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email, metadata={"user_id": str(user.id)}
    )
    user.stripe_customer_id = customer["id"]
    session.commit()
    return customer["id"]


def create_checkout_session(session: Session, user: User) -> str:
    """Create a Checkout Session for the Pro plan and return its URL."""
    _ensure_configured()
    customer_id = _ensure_customer(session, user)
    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(user.id),
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{settings.BASE_URL}/account?checkout=success",
        cancel_url=f"{settings.BASE_URL}/account?checkout=cancel",
        allow_promotion_codes=True,
    )
    return checkout["url"]


def create_portal_session(session: Session, user: User) -> str:
    """Create a Billing Portal session so the user can manage their plan."""
    _ensure_configured()
    if not user.stripe_customer_id:
        raise RuntimeError("User has no Stripe customer to manage.")
    portal = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{settings.BASE_URL}/account",
    )
    return portal["url"]


# --------------------------------------------------------------------------- #
# Webhook reconciliation
# --------------------------------------------------------------------------- #
def _apply_subscription_state(
    session: Session, *, customer_id: str, subscription_id: str | None, status: str | None
) -> None:
    user = (
        session.query(User).filter(User.stripe_customer_id == customer_id).one_or_none()
    )
    if user is None:
        log.warning("Webhook for unknown customer %s", customer_id)
        return

    user.stripe_subscription_id = subscription_id
    user.subscription_status = status
    user.plan = PLAN_PRO if (status in _ACTIVE_STATUSES) else PLAN_FREE
    session.commit()
    log.info("User %s -> plan=%s status=%s", user.email, user.plan, status)


def verify_and_parse(payload: bytes, sig_header: str | None) -> stripe.Event:
    """Verify a webhook signature and return the parsed event."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        # Without a signing secret we cannot trust the payload — reject.
        raise ValueError("Webhook secret not configured.")
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )


def handle_event(session: Session, event: stripe.Event) -> None:
    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        if customer_id:
            _apply_subscription_state(
                session,
                customer_id=customer_id,
                subscription_id=subscription_id,
                status="active",
            )
    elif etype in {"customer.subscription.updated", "customer.subscription.created"}:
        _apply_subscription_state(
            session,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("id"),
            status=obj.get("status"),
        )
    elif etype == "customer.subscription.deleted":
        _apply_subscription_state(
            session,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("id"),
            status="canceled",
        )
    else:
        log.debug("Ignoring Stripe event %s", etype)
