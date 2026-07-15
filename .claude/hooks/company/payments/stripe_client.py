#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["stripe"]
# ///
"""
Stripe Payment Integration — secure payment processing for Forge Teams.

P45 Implementation: Stripe Payment Integration.

This module provides Stripe integration with:
- Product and Price creation for Forge Teams tiers
- Checkout session creation (monthly/annual options)
- Webhook handling with HMAC signature verification
- Customer portal session creation
- Subscription lifecycle event handling
- Rate limiting and comprehensive audit logging

Credential Setup:
    Set environment variables:
    - STRIPE_SECRET_KEY: Stripe API secret key (required)
    - STRIPE_WEBHOOK_SECRET: Webhook signing secret (for verification)
    - STRIPE_PUBLISHABLE_KEY: Publishable key (for client reference)

Pricing Tiers (from forge-teams-pricing.md):
    - Starter: $99/mo, $948/yr (20% discount)
    - Pro: $249/mo, $2,390/yr (20% discount)
    - Business: $499/mo, $4,790/yr (20% discount)

Usage:
    from payments.stripe_client import StripeClient

    client = StripeClient()
    if client.initialize():
        # Create a checkout session
        session = client.create_checkout_session(
            tier="pro",
            billing_cycle="monthly",
            success_url="https://forge.dev/success",
            cancel_url="https://forge.dev/pricing",
        )
        if session.success:
            print(f"Checkout URL: {session.checkout_url}")

    # Handle webhook
    result = client.handle_webhook(payload, signature)
    if result.success:
        print(f"Event: {result.event_type}")
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stripe_client")


# =============================================================================
# Lazy imports
# =============================================================================

_stripe_module: Any = None


def _get_stripe():
    """Lazily import stripe module."""
    global _stripe_module
    if _stripe_module is None:
        try:
            import stripe

            _stripe_module = stripe
        except ImportError:
            raise ImportError(
                "stripe library is required for payment integration. "
                "Install with: pip install stripe"
            )
    return _stripe_module


# =============================================================================
# Constants
# =============================================================================


class PricingTier(str, Enum):
    """Forge Teams pricing tiers."""

    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"


class BillingCycle(str, Enum):
    """Billing cycle options."""

    MONTHLY = "monthly"
    ANNUAL = "annual"


# Pricing configuration (from forge-teams-pricing.md)
PRICING_CONFIG: dict[str, dict[str, Any]] = {
    PricingTier.STARTER.value: {
        "name": "Forge Teams Starter",
        "description": "For small teams getting started with AI-assisted development. Up to 10 developers.",
        "monthly_price_cents": 9900,  # $99.00
        "annual_price_cents": 94800,  # $948.00 (20% discount)
        "team_size_min": 5,
        "team_size_max": 10,
        "features": [
            "Shared agent library",
            "Centralized activity logs",
            "Team dashboard",
            "Email support (48h SLA)",
        ],
    },
    PricingTier.PRO.value: {
        "name": "Forge Teams Pro",
        "description": "For growing teams requiring governance and analytics. Up to 25 developers.",
        "monthly_price_cents": 24900,  # $249.00
        "annual_price_cents": 239000,  # $2,390.00 (20% discount)
        "team_size_min": 11,
        "team_size_max": 25,
        "features": [
            "Everything in Starter",
            "Usage analytics",
            "SSO integration",
            "Audit export (SOC 2)",
            "Custom hook library",
            "24h support SLA",
        ],
    },
    PricingTier.BUSINESS.value: {
        "name": "Forge Teams Business",
        "description": "For larger teams with enterprise-like requirements. Up to 50 developers.",
        "monthly_price_cents": 49900,  # $499.00
        "annual_price_cents": 479000,  # $4,790.00 (20% discount)
        "team_size_min": 26,
        "team_size_max": 50,
        "features": [
            "Everything in Pro",
            "Role-based access control (RBAC)",
            "Department-level configuration",
            "Knowledge base sharing",
            "Compliance templates",
            "4h support SLA + Slack channel",
        ],
    },
}

# Rate limiting configuration
DEFAULT_RATE_LIMIT_PER_MINUTE = 100
RATE_LIMIT_WINDOW_SECONDS = 60

# Default webhook timestamp tolerance (5 minutes)
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class StripeCredentials:
    """
    Stripe API credentials.

    Attributes:
        secret_key: Stripe API secret key
        webhook_secret: Webhook signing secret for verification
        publishable_key: Publishable key (optional, for client reference)
    """

    secret_key: str
    webhook_secret: Optional[str] = None
    publishable_key: Optional[str] = None

    def is_valid(self) -> bool:
        """Check if required credentials are present."""
        return bool(self.secret_key and self.secret_key.startswith("sk_"))


@dataclass
class CheckoutResult:
    """
    Result of checkout session creation.

    Attributes:
        success: Whether the operation succeeded
        session_id: Stripe session ID if successful
        checkout_url: URL to redirect customer to
        error: Error message if failed
    """

    success: bool
    session_id: Optional[str] = None
    checkout_url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class WebhookResult:
    """
    Result of webhook handling.

    Attributes:
        success: Whether the webhook was valid and processed
        event_type: Stripe event type (e.g., "checkout.session.completed")
        event_id: Stripe event ID
        data: Parsed event data
        error: Error message if failed
    """

    success: bool
    event_type: Optional[str] = None
    event_id: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class PortalResult:
    """
    Result of customer portal session creation.

    Attributes:
        success: Whether the operation succeeded
        portal_url: URL to redirect customer to
        error: Error message if failed
    """

    success: bool
    portal_url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class ProductResult:
    """
    Result of product/price creation.

    Attributes:
        success: Whether the operation succeeded
        product_id: Stripe product ID
        monthly_price_id: Stripe price ID for monthly billing
        annual_price_id: Stripe price ID for annual billing
        error: Error message if failed
    """

    success: bool
    product_id: Optional[str] = None
    monthly_price_id: Optional[str] = None
    annual_price_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class RateLimitState:
    """
    Track rate limit state for Stripe API.

    Attributes:
        requests_made: Number of requests in current window
        window_start: Unix timestamp when window started
    """

    requests_made: int = 0
    window_start: float = field(default_factory=time.time)


# =============================================================================
# Rate Limiter
# =============================================================================


class RateLimiter:
    """
    Thread-safe rate limiter for Stripe API calls.

    Attributes:
        max_per_minute: Maximum requests per minute
    """

    def __init__(self, max_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE):
        self.max_per_minute = max_per_minute
        self._state = RateLimitState()
        self._lock = threading.Lock()

    def can_proceed(self) -> bool:
        """
        Check if a request can proceed under rate limits.

        Returns:
            True if request is allowed, False if rate limited
        """
        with self._lock:
            now = time.time()

            # Reset window if expired
            if now - self._state.window_start >= RATE_LIMIT_WINDOW_SECONDS:
                self._state.requests_made = 0
                self._state.window_start = now

            if self._state.requests_made >= self.max_per_minute:
                return False

            self._state.requests_made += 1
            return True

    def get_status(self) -> dict[str, Any]:
        """Get current rate limit status."""
        with self._lock:
            now = time.time()
            remaining = max(0, self.max_per_minute - self._state.requests_made)
            seconds_until_reset = max(
                0, RATE_LIMIT_WINDOW_SECONDS - (now - self._state.window_start)
            )
            return {
                "remaining": remaining,
                "limit": self.max_per_minute,
                "seconds_until_reset": int(seconds_until_reset),
            }


# =============================================================================
# Stripe Client Implementation
# =============================================================================


class StripeClient:
    """
    Stripe API client for Forge Teams payment processing.

    Supports product/price management, checkout sessions, webhooks,
    and customer portal integration with rate limiting and audit logging.

    Attributes:
        credentials: Stripe API credentials
        initialized: Whether client is ready for API calls
        rate_limiter: Rate limiter for API calls

    Example:
        >>> client = StripeClient()
        >>> if client.initialize():
        ...     result = client.create_checkout_session(
        ...         tier="pro",
        ...         billing_cycle="monthly",
        ...         success_url="https://example.com/success",
        ...         cancel_url="https://example.com/cancel",
        ...     )
        ...     print(f"Checkout: {result.checkout_url}")
    """

    def __init__(
        self,
        company_dir: Optional[str | Path] = None,
        env_prefix: str = "STRIPE_",
    ):
        """
        Initialize Stripe client.

        Args:
            company_dir: Path to .company directory for logging
            env_prefix: Environment variable prefix for credentials
        """
        self.company_dir = (
            Path(company_dir) if company_dir else self._find_company_dir()
        )
        self.env_prefix = env_prefix

        self.credentials: Optional[StripeCredentials] = None
        self.initialized = False
        self.rate_limiter = RateLimiter()

        # Cache for created products/prices
        self._product_cache: dict[str, ProductResult] = {}

        # Load credentials on init
        self.credentials = self._load_credentials()

    def _find_company_dir(self) -> Path:
        """Find .company directory from current working directory."""
        cwd = Path.cwd()

        # Search up to root
        for parent in [cwd, *list(cwd.parents)]:
            company_dir = parent / ".company"
            if company_dir.is_dir():
                return company_dir

        # Default to cwd/.company
        return cwd / ".company"

    def _load_credentials(self) -> Optional[StripeCredentials]:
        """
        Load credentials from environment variables.

        Returns:
            StripeCredentials if found, None otherwise
        """
        secret_key = os.environ.get(f"{self.env_prefix}SECRET_KEY")
        webhook_secret = os.environ.get(f"{self.env_prefix}WEBHOOK_SECRET")
        publishable_key = os.environ.get(f"{self.env_prefix}PUBLISHABLE_KEY")

        if secret_key:
            logger.info(f"Loaded Stripe credentials with prefix: {self.env_prefix}")
            return StripeCredentials(
                secret_key=secret_key,
                webhook_secret=webhook_secret,
                publishable_key=publishable_key,
            )

        logger.warning(
            f"Stripe credentials not found. Set {self.env_prefix}SECRET_KEY "
            f"environment variable."
        )
        return None

    def initialize(self) -> bool:
        """
        Initialize Stripe API with credentials.

        Returns:
            True if initialization successful

        Example:
            >>> client = StripeClient()
            >>> if client.initialize():
            ...     print("Ready to process payments")
        """
        if not self.credentials or not self.credentials.is_valid():
            logger.error("Invalid or missing Stripe credentials")
            self._log_action("initialize", success=False, error="Missing credentials")
            return False

        try:
            stripe = _get_stripe()
            stripe.api_key = self.credentials.secret_key

            # Verify credentials with a simple API call
            stripe.Account.retrieve()

            self.initialized = True
            logger.info("Stripe client initialized successfully")
            self._log_action("initialize", success=True)
            return True

        except Exception as e:
            logger.exception(f"Stripe initialization error: {e}")
            self._log_action("initialize", success=False, error=str(e))
            return False

    # -------------------------------------------------------------------------
    # Product/Price Management
    # -------------------------------------------------------------------------

    def create_product_with_prices(self, tier: str | PricingTier) -> ProductResult:
        """
        Create a Stripe product with monthly and annual prices for a tier.

        Args:
            tier: Pricing tier (starter, pro, business)

        Returns:
            ProductResult with product and price IDs

        Example:
            >>> result = client.create_product_with_prices("pro")
            >>> if result.success:
            ...     print(f"Monthly: {result.monthly_price_id}")
            ...     print(f"Annual: {result.annual_price_id}")
        """
        tier_key = tier.value if isinstance(tier, PricingTier) else tier.lower()

        # Check cache
        if tier_key in self._product_cache:
            return self._product_cache[tier_key]

        if tier_key not in PRICING_CONFIG:
            return ProductResult(
                success=False,
                error=f"Unknown tier: {tier_key}. Valid: {list(PRICING_CONFIG.keys())}",
            )

        if not self.initialized:
            return ProductResult(success=False, error="Client not initialized")

        if not self.rate_limiter.can_proceed():
            return ProductResult(success=False, error="Rate limited")

        config = PRICING_CONFIG[tier_key]
        stripe = _get_stripe()

        try:
            # Create product
            product = stripe.Product.create(
                name=config["name"],
                description=config["description"],
                metadata={
                    "tier": tier_key,
                    "team_size_min": str(config["team_size_min"]),
                    "team_size_max": str(config["team_size_max"]),
                    "created_by": "forge_stripe_client",
                },
            )

            # Create monthly price
            monthly_price = stripe.Price.create(
                product=product.id,
                unit_amount=config["monthly_price_cents"],
                currency="usd",
                recurring={"interval": "month"},
                metadata={
                    "tier": tier_key,
                    "billing_cycle": "monthly",
                },
            )

            # Create annual price
            annual_price = stripe.Price.create(
                product=product.id,
                unit_amount=config["annual_price_cents"],
                currency="usd",
                recurring={"interval": "year"},
                metadata={
                    "tier": tier_key,
                    "billing_cycle": "annual",
                },
            )

            result = ProductResult(
                success=True,
                product_id=product.id,
                monthly_price_id=monthly_price.id,
                annual_price_id=annual_price.id,
            )

            # Cache result
            self._product_cache[tier_key] = result

            self._log_action(
                "create_product",
                success=True,
                details={
                    "tier": tier_key,
                    "product_id": product.id,
                    "monthly_price_id": monthly_price.id,
                    "annual_price_id": annual_price.id,
                },
            )

            return result

        except Exception as e:
            logger.exception(f"Failed to create product for tier {tier_key}: {e}")
            self._log_action(
                "create_product",
                success=False,
                error=str(e),
                details={"tier": tier_key},
            )
            return ProductResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Checkout Sessions
    # -------------------------------------------------------------------------

    def create_checkout_session(
        self,
        tier: str | PricingTier,
        billing_cycle: str | BillingCycle,
        success_url: str,
        cancel_url: str,
        customer_email: Optional[str] = None,
        customer_id: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        price_id: Optional[str] = None,
    ) -> CheckoutResult:
        """
        Create a Stripe checkout session for subscription.

        Args:
            tier: Pricing tier (starter, pro, business)
            billing_cycle: Monthly or annual
            success_url: URL to redirect on successful checkout
            cancel_url: URL to redirect on cancelled checkout
            customer_email: Pre-fill customer email (optional)
            customer_id: Existing Stripe customer ID (optional)
            metadata: Additional metadata to attach (optional)
            price_id: Override price ID (optional, uses tier default)

        Returns:
            CheckoutResult with session ID and checkout URL

        Example:
            >>> result = client.create_checkout_session(
            ...     tier="pro",
            ...     billing_cycle="monthly",
            ...     success_url="https://forge.dev/success?session_id={CHECKOUT_SESSION_ID}",
            ...     cancel_url="https://forge.dev/pricing",
            ...     customer_email="user@example.com",
            ... )
            >>> if result.success:
            ...     print(f"Redirect to: {result.checkout_url}")
        """
        tier_key = tier.value if isinstance(tier, PricingTier) else tier.lower()
        cycle_key = (
            billing_cycle.value
            if isinstance(billing_cycle, BillingCycle)
            else billing_cycle.lower()
        )

        if not self.initialized:
            return CheckoutResult(success=False, error="Client not initialized")

        if not self.rate_limiter.can_proceed():
            return CheckoutResult(success=False, error="Rate limited")

        # Resolve price_id
        if not price_id:
            if tier_key not in PRICING_CONFIG:
                return CheckoutResult(
                    success=False,
                    error=f"Unknown tier: {tier_key}",
                )

            # Ensure product exists
            product_result = self.create_product_with_prices(tier_key)
            if not product_result.success:
                return CheckoutResult(
                    success=False,
                    error=f"Failed to create product: {product_result.error}",
                )

            price_id = (
                product_result.monthly_price_id
                if cycle_key == "monthly"
                else product_result.annual_price_id
            )

        stripe = _get_stripe()

        try:
            session_params: dict[str, Any] = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": {
                    "tier": tier_key,
                    "billing_cycle": cycle_key,
                    "created_by": "forge_stripe_client",
                    **(metadata or {}),
                },
            }

            if customer_email:
                session_params["customer_email"] = customer_email

            if customer_id:
                session_params["customer"] = customer_id

            session = stripe.checkout.Session.create(**session_params)

            result = CheckoutResult(
                success=True,
                session_id=session.id,
                checkout_url=session.url,
            )

            self._log_action(
                "create_checkout_session",
                success=True,
                details={
                    "session_id": session.id,
                    "tier": tier_key,
                    "billing_cycle": cycle_key,
                },
            )

            return result

        except Exception as e:
            logger.exception(f"Failed to create checkout session: {e}")
            self._log_action(
                "create_checkout_session",
                success=False,
                error=str(e),
                details={"tier": tier_key, "billing_cycle": cycle_key},
            )
            return CheckoutResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Webhook Handling
    # -------------------------------------------------------------------------

    def handle_webhook(
        self,
        payload: bytes,
        signature: str,
    ) -> WebhookResult:
        """
        Handle incoming Stripe webhook with signature verification.

        Verifies the webhook signature using HMAC-SHA256 and processes
        subscription lifecycle events.

        Args:
            payload: Raw request body as bytes
            signature: Stripe-Signature header value

        Returns:
            WebhookResult with event type and data

        Supported Events:
            - checkout.session.completed: Customer completed checkout
            - customer.subscription.created: New subscription created
            - customer.subscription.updated: Subscription changed
            - customer.subscription.deleted: Subscription cancelled
            - invoice.paid: Invoice successfully paid
            - invoice.payment_failed: Invoice payment failed

        Example:
            >>> # In your webhook endpoint
            >>> result = client.handle_webhook(
            ...     payload=request.body,
            ...     signature=request.headers["Stripe-Signature"],
            ... )
            >>> if result.success:
            ...     if result.event_type == "checkout.session.completed":
            ...         session = result.data["object"]
            ...         customer_id = session["customer"]
            ...         # Provision access for customer
        """
        if not self.credentials or not self.credentials.webhook_secret:
            return WebhookResult(
                success=False,
                error="Webhook secret not configured",
            )

        stripe = _get_stripe()

        try:
            # Verify signature and construct event
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                self.credentials.webhook_secret,
            )

            event_type = event["type"]
            event_id = event["id"]
            event_data = event["data"]

            # Log the event
            self._log_action(
                "webhook_received",
                success=True,
                details={
                    "event_type": event_type,
                    "event_id": event_id,
                },
            )

            # Process subscription lifecycle events
            self._process_subscription_event(event_type, event_data)

            return WebhookResult(
                success=True,
                event_type=event_type,
                event_id=event_id,
                data=event_data,
            )

        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Webhook signature verification failed: {e}")
            self._log_action(
                "webhook_received",
                success=False,
                error="Signature verification failed",
            )
            return WebhookResult(
                success=False,
                error="Invalid signature",
            )

        except Exception as e:
            logger.exception(f"Webhook processing error: {e}")
            self._log_action(
                "webhook_received",
                success=False,
                error=str(e),
            )
            return WebhookResult(success=False, error=str(e))

    def verify_webhook_signature(
        self,
        payload: bytes,
        signature: str,
    ) -> bool:
        """
        Verify webhook signature without processing the event.

        Uses constant-time comparison to prevent timing attacks.

        Args:
            payload: Raw request body as bytes
            signature: Stripe-Signature header value

        Returns:
            True if signature is valid

        Example:
            >>> if client.verify_webhook_signature(payload, sig):
            ...     # Signature valid, safe to process
        """
        if not self.credentials or not self.credentials.webhook_secret:
            return False

        stripe = _get_stripe()

        try:
            stripe.Webhook.construct_event(
                payload,
                signature,
                self.credentials.webhook_secret,
            )
            return True
        except stripe.error.SignatureVerificationError:
            return False

    def _process_subscription_event(
        self,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        """
        Process subscription lifecycle events internally.

        Args:
            event_type: Stripe event type
            event_data: Event data payload
        """
        obj = event_data.get("object", {})

        if event_type == "checkout.session.completed":
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            logger.info(
                f"Checkout completed: customer={customer_id}, "
                f"subscription={subscription_id}"
            )

        elif event_type == "customer.subscription.created":
            subscription_id = obj.get("id")
            status = obj.get("status")
            logger.info(f"Subscription created: {subscription_id}, status={status}")

        elif event_type == "customer.subscription.updated":
            subscription_id = obj.get("id")
            status = obj.get("status")
            logger.info(f"Subscription updated: {subscription_id}, status={status}")

        elif event_type == "customer.subscription.deleted":
            subscription_id = obj.get("id")
            logger.info(f"Subscription cancelled: {subscription_id}")

        elif event_type == "invoice.paid":
            invoice_id = obj.get("id")
            amount_paid = obj.get("amount_paid", 0)
            logger.info(f"Invoice paid: {invoice_id}, amount={amount_paid}")

        elif event_type == "invoice.payment_failed":
            invoice_id = obj.get("id")
            customer_id = obj.get("customer")
            logger.warning(
                f"Invoice payment failed: {invoice_id}, customer={customer_id}"
            )

    # -------------------------------------------------------------------------
    # Customer Portal
    # -------------------------------------------------------------------------

    def create_customer_portal_session(
        self,
        customer_id: str,
        return_url: str,
    ) -> PortalResult:
        """
        Create a Stripe customer portal session.

        The customer portal allows customers to manage their subscription,
        update payment methods, and view invoices.

        Args:
            customer_id: Stripe customer ID
            return_url: URL to redirect when customer exits portal

        Returns:
            PortalResult with portal URL

        Example:
            >>> result = client.create_customer_portal_session(
            ...     customer_id="cus_xxx",
            ...     return_url="https://forge.dev/account",
            ... )
            >>> if result.success:
            ...     # Redirect customer to result.portal_url
        """
        if not self.initialized:
            return PortalResult(success=False, error="Client not initialized")

        if not self.rate_limiter.can_proceed():
            return PortalResult(success=False, error="Rate limited")

        stripe = _get_stripe()

        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )

            self._log_action(
                "create_portal_session",
                success=True,
                details={"customer_id": customer_id},
            )

            return PortalResult(
                success=True,
                portal_url=session.url,
            )

        except Exception as e:
            logger.exception(f"Failed to create portal session: {e}")
            self._log_action(
                "create_portal_session",
                success=False,
                error=str(e),
                details={"customer_id": customer_id},
            )
            return PortalResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def get_pricing_info(self, tier: Optional[str] = None) -> dict[str, Any]:
        """
        Get pricing information for tiers.

        Args:
            tier: Specific tier to query, or None for all tiers

        Returns:
            Dictionary with pricing details

        Example:
            >>> info = client.get_pricing_info("pro")
            >>> print(f"Monthly: ${info['monthly_price_cents'] / 100}")
        """
        if tier:
            tier_key = tier.lower()
            if tier_key in PRICING_CONFIG:
                return PRICING_CONFIG[tier_key].copy()
            return {"error": f"Unknown tier: {tier}"}
        return {k: v.copy() for k, v in PRICING_CONFIG.items()}

    def get_rate_limit_status(self) -> dict[str, Any]:
        """
        Get current rate limit status.

        Returns:
            Dictionary with rate limit info
        """
        return self.rate_limiter.get_status()

    # -------------------------------------------------------------------------
    # Audit Logging
    # -------------------------------------------------------------------------

    def _log_action(
        self,
        action: str,
        success: bool,
        error: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Log action to audit file.

        Args:
            action: Action name
            success: Whether action succeeded
            error: Error message if failed
            details: Additional details to log (secrets redacted)
        """
        log_dir = self.company_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "stripe_client.log"

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "success": success,
            "platform": "stripe",
        }

        if error:
            entry["error"] = error
        if details:
            # Redact sensitive fields
            safe_details = {
                k: v
                for k, v in details.items()
                if k not in ("secret_key", "webhook_secret", "api_key")
            }
            entry["details"] = safe_details

        # Rate limit status
        entry["rate_limits"] = self.rate_limiter.get_status()

        # WS-091: Use fcntl.flock to prevent concurrent write interleaving
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(entry) + "\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except OSError as e:
            logger.error(f"Failed to write audit log: {e}")


# =============================================================================
# Module-Level Convenience Functions
# =============================================================================


def create_checkout_session(
    tier: str,
    billing_cycle: str,
    success_url: str,
    cancel_url: str,
    customer_email: Optional[str] = None,
    price_id: Optional[str] = None,
) -> CheckoutResult:
    """
    Create a Stripe checkout session (convenience function).

    Initializes a client and creates a checkout session in one call.

    Args:
        tier: Pricing tier (starter, pro, business)
        billing_cycle: Monthly or annual
        success_url: URL to redirect on successful checkout
        cancel_url: URL to redirect on cancelled checkout
        customer_email: Pre-fill customer email (optional)
        price_id: Override price ID (optional)

    Returns:
        CheckoutResult with session ID and checkout URL

    Example:
        >>> from payments.stripe_client import create_checkout_session
        >>> result = create_checkout_session(
        ...     tier="pro",
        ...     billing_cycle="monthly",
        ...     success_url="https://example.com/success",
        ...     cancel_url="https://example.com/cancel",
        ... )
    """
    client = StripeClient()
    if not client.initialize():
        return CheckoutResult(success=False, error="Failed to initialize Stripe client")

    return client.create_checkout_session(
        tier=tier,
        billing_cycle=billing_cycle,
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=customer_email,
        price_id=price_id,
    )


def handle_webhook(payload: bytes, signature: str) -> WebhookResult:
    """
    Handle incoming Stripe webhook (convenience function).

    Args:
        payload: Raw request body as bytes
        signature: Stripe-Signature header value

    Returns:
        WebhookResult with event type and data

    Example:
        >>> from payments.stripe_client import handle_webhook
        >>> result = handle_webhook(request.body, request.headers["Stripe-Signature"])
        >>> if result.success:
        ...     print(f"Received event: {result.event_type}")
    """
    client = StripeClient()
    # Note: For webhook handling, we don't need to initialize (which calls Account.retrieve)
    # We just need credentials loaded
    if not client.credentials or not client.credentials.webhook_secret:
        return WebhookResult(success=False, error="Webhook secret not configured")

    return client.handle_webhook(payload, signature)


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> None:
    """CLI entry point for testing."""
    import sys

    if len(sys.argv) < 2:
        print(
            """
Stripe Client — Forge Payment Integration

Commands:
    init            Test Stripe connection
    pricing         Show pricing tiers
    create-product TIER  Create product and prices for tier
    status          Show rate limit status

Examples:
    python stripe_client.py init
    python stripe_client.py pricing
    python stripe_client.py create-product pro
"""
        )
        sys.exit(0)

    command = sys.argv[1].lower()
    client = StripeClient()

    if command == "init":
        if client.initialize():
            print("Stripe client initialized successfully")
        else:
            print("Failed to initialize Stripe client")
            sys.exit(1)

    elif command == "pricing":
        print("Forge Teams Pricing:\n")
        for tier, config in PRICING_CONFIG.items():
            monthly = config["monthly_price_cents"] / 100
            annual = config["annual_price_cents"] / 100
            print(f"{config['name']}:")
            print(f"  Monthly: ${monthly:.2f}/mo")
            print(
                f"  Annual:  ${annual:.2f}/yr ({100 - (annual / (monthly * 12) * 100):.0f}% savings)"
            )
            print(
                f"  Team size: {config['team_size_min']}-{config['team_size_max']} developers"
            )
            print()

    elif command == "create-product":
        if len(sys.argv) < 3:
            print("Usage: python stripe_client.py create-product TIER")
            sys.exit(1)

        tier = sys.argv[2]
        if not client.initialize():
            print("Failed to initialize Stripe client")
            sys.exit(1)

        result = client.create_product_with_prices(tier)
        if result.success:
            print(f"Created product: {result.product_id}")
            print(f"  Monthly price: {result.monthly_price_id}")
            print(f"  Annual price: {result.annual_price_id}")
        else:
            print(f"Failed: {result.error}")
            sys.exit(1)

    elif command == "status":
        status = client.get_rate_limit_status()
        print("Rate Limit Status:")
        print(f"  Remaining: {status['remaining']}/{status['limit']}")
        print(f"  Resets in: {status['seconds_until_reset']}s")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
