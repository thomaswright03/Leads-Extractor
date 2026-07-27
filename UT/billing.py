"""Stripe billing: Checkout Session creation and webhook event handling.
Grants/revokes county or state-wide access based on subscription lifecycle
events. Stdlib-only (urllib), same pattern as mailer.py - no stripe pip
package, including the webhook signature check implemented by hand.

Reads STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET from the environment -
never hardcoded. STRIPE_SECRET_KEY is the test-mode secret key
(sk_test_...) until the user is ready to go live.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pipeline_db

STRIPE_API_URL = "https://api.stripe.com/v1"
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


class BillingError(Exception):
    """User-facing message - bad plan, Stripe API error, or webhook
    signature failure."""


def _stripe_request(method, path, data=None):
    if not STRIPE_SECRET_KEY:
        raise BillingError("STRIPE_SECRET_KEY is not set.")
    auth = base64.b64encode(f"{STRIPE_SECRET_KEY}:".encode("ascii")).decode("ascii")
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(
        f"{STRIPE_API_URL}/{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(error_body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = error_body or str(e)
        raise BillingError(message) from e
    except urllib.error.URLError as e:
        raise BillingError(f"Couldn't reach Stripe: {e.reason}") from e


def create_checkout_session(email, kind, key, success_url, cancel_url):
    """kind is 'county' or 'state_bundle', key is the county folder or state
    abbreviation. Returns the Checkout Session's hosted URL to redirect the
    browser to."""
    plan = pipeline_db.get_pricing_plan(kind, key)
    if not plan or not plan.get("stripe_price_id"):
        raise BillingError("That plan isn't set up yet.")

    session = _stripe_request("POST", "checkout/sessions", {
        "mode": "subscription",
        "customer_email": email,
        "line_items[0][price]": plan["stripe_price_id"],
        "line_items[0][quantity]": 1,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[email]": email,
        "metadata[kind]": kind,
        "metadata[key]": key,
        "subscription_data[metadata][email]": email,
        "subscription_data[metadata][kind]": kind,
        "subscription_data[metadata][key]": key,
    })
    return session["url"]


def _verify_webhook_signature(payload_bytes, sig_header):
    """Stripe's own signature scheme, implemented by hand instead of pulling
    in the stripe pip package for just this: the Stripe-Signature header is
    "t=<unix ts>,v1=<hex hmac>"; the signed content is "{ts}.{raw body}",
    HMAC-SHA256'd with the webhook's signing secret."""
    if not STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET is not set.")
    if not sig_header:
        raise BillingError("Missing Stripe-Signature header.")

    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        raise BillingError("Malformed Stripe-Signature header.")

    signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise BillingError("Webhook signature verification failed.")

    # Stripe's own recommended tolerance against replay attacks.
    if abs(time.time() - int(timestamp)) > 300:
        raise BillingError("Webhook timestamp too old.")


def handle_webhook_event(payload_bytes, sig_header):
    """Verifies the signature, then grants/revokes access based on the
    event type. Returns the event type handled. Raises BillingError on a
    bad signature - the caller should respond 400, not 200, so Stripe knows
    to retry only genuine delivery failures, not silently accept forgeries."""
    _verify_webhook_signature(payload_bytes, sig_header)
    event = json.loads(payload_bytes)
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        email = metadata.get("email") or obj.get("customer_email")
        kind = metadata.get("kind")
        key = metadata.get("key")
        subscription_id = obj.get("subscription")
        customer_id = obj.get("customer")
        if email and kind and key and subscription_id:
            pipeline_db.record_subscription(email, kind, key, customer_id, subscription_id, "active")
            if kind == "county":
                pipeline_db.grant_county_access(email, key)
            elif kind == "state_bundle":
                pipeline_db.grant_state_access(email, key)

    elif event_type == "customer.subscription.deleted":
        subscription_id = obj["id"]
        record = pipeline_db.get_subscription_by_stripe_id(subscription_id)
        if record:
            pipeline_db.update_subscription_status(subscription_id, "canceled")
            if record["kind"] == "county":
                pipeline_db.revoke_county_access(record["email"], record["key"])
            elif record["kind"] == "state_bundle":
                pipeline_db.revoke_state_access(record["email"], record["key"])

    return event_type
