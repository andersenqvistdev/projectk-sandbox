# /// script
# requires-python = ">=3.10"
# dependencies = ["stripe"]
# ///
"""
Forge License Webhook Server

Minimal HTTP server that receives Stripe webhook events and generates
Ed25519-signed license files on successful checkout.

Flow:
  1. Stripe sends checkout.session.completed event
  2. Server verifies HMAC-SHA256 signature
  3. Extracts customer email + tier from session metadata
  4. Calls forge_issue_license.py to generate signed license
  5. Saves license to .company/licenses/<customer-email>.json
  6. (Future: emails license to customer)

Usage:
  # Start webhook server on port 4242
  uv run .claude/hooks/company/payments/webhook_server.py

  # With custom port
  FORGE_WEBHOOK_PORT=8080 uv run .claude/hooks/company/payments/webhook_server.py

Environment:
  FORGE_STRIPE_SECRET_KEY       Stripe secret key (sk_...)
  FORGE_STRIPE_WEBHOOK_SECRET   Webhook signing secret (whsec_...)
  FORGE_LICENSE_PRIVATE_KEY     Ed25519 private key (base64 PEM) for signing
  FORGE_WEBHOOK_PORT            Server port (default: 4242)

Security hardening (PURCHASE_FLOW_AUDIT findings):
  C4  MAX_PAYLOAD_BYTES: rejects oversized POST bodies before reading (OOM DoS)
  C5  Generic error responses: internal Stripe exception details logged server-side only
  H4  500 on license failure: when private key is configured, generation failure returns
      HTTP 500 so Stripe retries delivery instead of silently losing the event
  H5  Idempotency: processed Stripe event IDs are persisted; duplicate deliveries are
      acknowledged (200) but not re-processed
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Stripe import
try:
    import stripe
except ImportError:
    print(
        "ERROR: stripe package required. Install with: uv pip install stripe",
        file=sys.stderr,
    )
    sys.exit(1)

# Configuration
WEBHOOK_PORT = int(os.environ.get("FORGE_WEBHOOK_PORT", "4242"))
STRIPE_SECRET = os.environ.get("FORGE_STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("FORGE_STRIPE_WEBHOOK_SECRET", "")
LICENSE_PRIVATE_KEY = os.environ.get("FORGE_LICENSE_PRIVATE_KEY", "")
LICENSE_PRIVATE_KEY_PATH = os.environ.get("FORGE_LICENSE_PRIVATE_KEY_PATH", "")

# Project root (for finding forge_issue_license.py).
# parents[4]: payments -> company -> hooks -> .claude -> repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[4]

# License output directory
LICENSE_DIR = PROJECT_ROOT / ".company" / "licenses"

# C4: Maximum POST body size (65 536 bytes). Stripe webhook payloads are
# typically < 2 KB; this limit prevents OOM DoS via oversized Content-Length.
MAX_PAYLOAD_BYTES = 65_536

# H5: Idempotency — file that stores processed Stripe event IDs so duplicate
# webhook deliveries (Stripe retries on timeout/error) are not re-processed.
_PROCESSED_EVENTS_FILE = (
    PROJECT_ROOT / ".company" / "state" / "processed_webhook_events.json"
)
# Cap the number of stored event IDs to avoid unbounded growth.
_MAX_STORED_EVENTS = 1_000


def _is_event_processed(event_id: str) -> bool:
    """Return True if this Stripe event ID has already been handled (H5)."""
    import fcntl

    path = _PROCESSED_EVENTS_FILE
    if not path.exists():
        return False
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return event_id in data
    except Exception:
        return False


def _mark_event_processed(event_id: str) -> None:
    """Record a Stripe event ID as processed (H5). Thread/process safe via flock."""
    import fcntl

    path = _PROCESSED_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open for read+write, creating if absent.
    try:
        flag = os.O_RDWR | os.O_CREAT
        fd = os.open(str(path), flag, 0o600)
    except OSError as e:
        print(f"[Idempotency] Cannot open events file: {e}", file=sys.stderr)
        return

    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()
            data: dict = json.loads(raw) if raw.strip() else {}
            data[event_id] = datetime.now(timezone.utc).isoformat()
            # Evict oldest entries when over the cap.
            if len(data) > _MAX_STORED_EVENTS:
                oldest = sorted(data.items(), key=lambda kv: kv[1])
                data = dict(oldest[-_MAX_STORED_EVENTS:])
            f.seek(0)
            f.truncate()
            json.dump(data, f)
        except Exception as e:
            print(f"[Idempotency] Write failed: {e}", file=sys.stderr)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def generate_license(email: str, tier: str, org_name: str = "") -> dict:
    """Generate a signed license file for a customer.

    Args:
        email: Customer email (used as org_id)
        tier: License tier (teams-starter, teams-pro, teams-business)
        org_name: Organization name (defaults to email prefix)

    Returns:
        Dict with success status and license path
    """
    if not org_name:
        org_name = email.split("@")[0].replace(".", " ").title()

    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", email.replace("@", "_at_"))
    org_id = f"org_{sanitized}"
    out_path = LICENSE_DIR / f"{org_id}.json"
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)

    issuer_path = (
        PROJECT_ROOT / ".claude" / "hooks" / "company" / "forge_issue_license.py"
    )

    cmd = [
        "uv",
        "run",
        str(issuer_path),
        "--org",
        org_name,
        "--org-id",
        org_id,
        "--tier",
        tier,
        "--valid-until",
        "perpetual",
        "--out",
        str(out_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            return {
                "success": True,
                "license_path": str(out_path),
                "org_id": org_id,
                "org_name": org_name,
                "tier": tier,
            }
        print(
            f"[License] Generation failed: {result.stderr[:200]}",
            file=sys.stderr,
        )
        return {
            "success": False,
            "error": "License generation failed. Please contact support.",
        }
    except Exception as e:
        print(f"[License] Exception: {e}", file=sys.stderr)
        return {
            "success": False,
            "error": "License generation failed. Please contact support.",
        }


def _write_license_meta(
    license_dir: Path,
    org_id: str,
    customer_email: str,
    stripe_session_id: str,
) -> None:
    """Write a companion .meta.json file alongside the issued license."""
    license_dir.mkdir(parents=True, exist_ok=True)
    meta_path = license_dir / f"{org_id}.meta.json"
    meta = {
        "org_id": org_id,
        "customer_email": customer_email,
        "stripe_session_id": stripe_session_id,
        "touchpoints_sent": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)


def _enqueue_pending_email_queue(
    project_root: Path,
    org_id: str,
    org_name: str,
    customer_email: str,
    tier: str,
) -> None:
    """Append a welcome touchpoint entry to pending_emails.json."""
    queue_path = project_root / ".company" / "state" / "pending_emails.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "org_id": org_id,
        "org_name": org_name,
        "customer_email": customer_email,
        "tier": tier,
        "touchpoint": "welcome",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    existing: list = []
    if queue_path.exists():
        try:
            with open(queue_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []
    existing.append(entry)
    with open(queue_path, "w") as f:
        json.dump(existing, f, indent=2)


def log_event(event_type: str, data: dict):
    """Append webhook event to audit log.

    WS-091: Uses fcntl.flock to prevent concurrent write interleaving.
    """
    import fcntl

    log_path = PROJECT_ROOT / ".company" / "external_audit.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    with open(log_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


CORS_ORIGIN = os.environ.get("FORGE_CORS_ORIGIN", "https://forgeframework.dev")

# Allowed license tiers — reject anything not in this set
ALLOWED_TIERS = {"teams-starter", "teams-pro", "teams-business"}

# Tier to Stripe price mapping (set after creating products in Stripe dashboard)
# Override via env: FORGE_STRIPE_PRICE_STARTER=price_xxx
PRICE_IDS = {
    "teams-starter": os.environ.get("FORGE_STRIPE_PRICE_STARTER", ""),
    "teams-pro": os.environ.get("FORGE_STRIPE_PRICE_PRO", ""),
    "teams-business": os.environ.get("FORGE_STRIPE_PRICE_BUSINESS", ""),
}


class WebhookHandler(BaseHTTPRequestHandler):
    """Handle Stripe webhook and checkout API requests."""

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        # C4: Reject oversized request bodies before reading.
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_PAYLOAD_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Payload too large"}')
            return

        payload = self.rfile.read(content_length)

        if self.path == "/create-checkout-session":
            self._handle_create_checkout(payload)
            return

        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        sig_header = self.headers.get("Stripe-Signature", "")

        # Verify signature
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            log_event("webhook_rejected", {"reason": "invalid_signature"})
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid signature"}')
            return
        except Exception as e:
            # C5: Log details server-side; return generic message to caller.
            print(f"[Webhook] Event parse error: {e}", file=sys.stderr)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid webhook payload"}')
            return

        event_type = event["type"]
        event_id = event.get("id", "")
        print(f"[Webhook] Received: {event_type} ({event_id})", file=sys.stderr)

        # H5: Idempotency check — skip already-processed events.
        if event_id and _is_event_processed(event_id):
            print(f"[Webhook] Duplicate event skipped: {event_id}", file=sys.stderr)
            log_event(
                "webhook_duplicate",
                {"event_id": event_id, "event_type": event_type},
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"received": true, "duplicate": true}')
            return

        # Handle checkout completion
        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            customer_email = session.get(
                "customer_email",
                session.get("customer_details", {}).get("email", ""),
            )
            metadata = session.get("metadata", {})
            raw_tier = metadata.get("tier", "teams-starter")
            tier = raw_tier if raw_tier in ALLOWED_TIERS else "teams-starter"
            org_name = metadata.get("org_name", "")

            if raw_tier not in ALLOWED_TIERS:
                log_event(
                    "tier_rejected",
                    {"requested_tier": raw_tier, "fallback": tier},
                )

            # C7 (W7): Do not log customer email to stderr/audit in plaintext.
            print(
                f"[Webhook] Checkout completed: tier={tier}",
                file=sys.stderr,
            )

            # Validate email before license generation
            if (
                not customer_email
                or "@" not in customer_email
                or len(customer_email) > 254
            ):
                log_event(
                    "license_skipped",
                    {
                        "email": customer_email[:50] if customer_email else "",
                        "tier": tier,
                        "reason": "invalid_email",
                    },
                )
            elif customer_email and (LICENSE_PRIVATE_KEY or LICENSE_PRIVATE_KEY_PATH):
                # Private key is configured — generate license and return 500 on
                # failure so Stripe retries the delivery (H4).
                license_result = generate_license(customer_email, tier, org_name)
                log_event(
                    "license_generated",
                    {
                        "tier": tier,
                        **license_result,
                    },
                )

                if not license_result.get("success"):
                    # H4: Signal Stripe to retry by returning 500.
                    print(
                        "[Webhook] License generation failed — returning 500 for Stripe retry",
                        file=sys.stderr,
                    )
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "License generation failed"}')
                    return

                print(f"[Webhook] License issued for tier={tier}", file=sys.stderr)
                _write_license_meta(
                    license_dir=LICENSE_DIR,
                    org_id=license_result["org_id"],
                    customer_email=customer_email,
                    stripe_session_id=event_id,
                )
                _enqueue_pending_email_queue(
                    project_root=PROJECT_ROOT,
                    org_id=license_result["org_id"],
                    org_name=license_result["org_name"],
                    customer_email=customer_email,
                    tier=tier,
                )
            else:
                log_event(
                    "license_skipped",
                    {
                        "tier": tier,
                        "reason": "no_private_key"
                        if not LICENSE_PRIVATE_KEY
                        else "no_email",
                    },
                )

        elif event_type in (
            "customer.subscription.deleted",
            "customer.subscription.updated",
        ):
            log_event(
                event_type,
                {"data": str(event["data"]["object"].get("id", ""))[:50]},
            )

        elif event_type == "invoice.payment_failed":
            raw_email = str(event["data"]["object"].get("customer_email", ""))
            if "@" in raw_email:
                redacted = f"[redacted]@{raw_email.split('@', 1)[1]}"
            else:
                redacted = "[redacted]"
            log_event("payment_failed", {"data": redacted[:50]})

        # Mark event as processed (H5).
        if event_id:
            _mark_event_processed(event_id)

        # Respond 200 to Stripe
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def _handle_create_checkout(self, payload: bytes):
        """Create a Stripe Checkout Session and return the URL."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid JSON"}')
            return

        tier = data.get("tier", "teams-starter")

        if tier not in ALLOWED_TIERS:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid tier."}')
            return

        price_id = PRICE_IDS.get(tier)

        if not price_id:
            self.send_response(400)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(
                b'{"error": "This tier is not currently available for purchase."}',
            )
            return

        try:
            session = stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=(
                    f"{CORS_ORIGIN}/success.html?session_id={{CHECKOUT_SESSION_ID}}"
                ),
                cancel_url=f"{CORS_ORIGIN}/teams.html#pricing",
                metadata={"tier": tier},
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"url": session.url}).encode())
            log_event("checkout_created", {"tier": tier, "session_id": session.id})
        except Exception as e:
            # C5: Log details server-side; return generic message to caller.
            print(f"[Checkout] Stripe error: {e}", file=sys.stderr)
            self.send_response(500)
            self._cors_headers()
            self.end_headers()
            self.wfile.write(
                b'{"error": "Checkout session creation failed. Please try again."}',
            )

    def do_GET(self):
        """Health check endpoint."""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "ok",
                        "service": "forge-webhook",
                        "stripe_configured": bool(STRIPE_SECRET),
                        "webhook_secret_configured": bool(WEBHOOK_SECRET),
                        "license_key_configured": bool(
                            LICENSE_PRIVATE_KEY or LICENSE_PRIVATE_KEY_PATH
                        ),
                    }
                ).encode()
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default access logs, use stderr."""
        pass


def main():
    if not STRIPE_SECRET:
        print("WARNING: FORGE_STRIPE_SECRET_KEY not set", file=sys.stderr)
    if not WEBHOOK_SECRET:
        print(
            "FATAL: FORGE_STRIPE_WEBHOOK_SECRET not set — "
            "webhook signature verification disabled. Refusing to start.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not LICENSE_PRIVATE_KEY and not LICENSE_PRIVATE_KEY_PATH:
        print(
            "WARNING: FORGE_LICENSE_PRIVATE_KEY not set — licenses won't be signed",
            file=sys.stderr,
        )

    stripe.api_key = STRIPE_SECRET

    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    print(f"Forge Webhook Server running on port {WEBHOOK_PORT}", file=sys.stderr)
    print(f"  Endpoint: POST http://localhost:{WEBHOOK_PORT}/webhook", file=sys.stderr)
    print(f"  Health:   GET  http://localhost:{WEBHOOK_PORT}/health", file=sys.stderr)
    print(
        f"  Stripe:   {'configured' if STRIPE_SECRET else 'NOT configured'}",
        file=sys.stderr,
    )
    print(
        f"  Signing:  {'configured' if LICENSE_PRIVATE_KEY or LICENSE_PRIVATE_KEY_PATH else 'NOT configured'}",
        file=sys.stderr,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down webhook server", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
