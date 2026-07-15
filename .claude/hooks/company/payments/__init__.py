"""
Payments Integration — Forge payment processing clients.

P45 Implementation: Stripe Payment Integration.

This package provides Stripe integration for payment processing:
- StripeClient: Main client for Stripe API interactions
- Checkout session creation for one-time and subscription payments
- Webhook handling for payment events

Usage:
    from payments import StripeClient, create_checkout_session

    client = StripeClient()
    session = create_checkout_session(
        price_id="price_xxx",
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel"
    )
"""

from .stripe_client import (
    StripeClient,
    create_checkout_session,
    handle_webhook,
)

__all__ = [
    "StripeClient",
    "create_checkout_session",
    "handle_webhook",
]
