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


def create_checkout_session(email, kind, key, success_url, cancel_url, extra_metadata=None):
    """kind is 'county' or 'state_bundle', key is the county folder or state
    abbreviation. extra_metadata (e.g. a pending-signup token) is stamped
    onto the Checkout Session alongside the standard fields, so
    handle_webhook_event can read it back off obj["metadata"] once payment
    is confirmed. Returns the Checkout Session's hosted URL to redirect the
    browser to."""
    plan = pipeline_db.get_pricing_plan(kind, key)
    if not plan or not plan.get("stripe_price_id"):
        raise BillingError("That plan isn't set up yet.")

    fields = {
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
    }
    for meta_key, meta_value in (extra_metadata or {}).items():
        fields[f"metadata[{meta_key}]"] = meta_value

    session = _stripe_request("POST", "checkout/sessions", fields)
    return session["url"]


def update_plan_price(kind, key, monthly_price_cents):
    """Changes what a plan actually costs, everywhere - Stripe Prices are
    immutable once created, so "changing" one really means creating a new
    Price at the new amount and pointing the plan at it; the old Price gets
    archived (active=false) so it can't be reused, best-effort since a
    failure there doesn't leave anything actually wrong, just an unused
    Price object sitting in Stripe. Every checkout going forward reads the
    plan's stripe_price_id fresh out of Supabase (see create_checkout_session),
    so this is the one place that needs to change for a new price to take
    effect at signup, "Add More Access", and anywhere else that quotes it."""
    plan = pipeline_db.get_pricing_plan(kind, key)
    if not plan:
        raise BillingError("No pricing plan exists yet for this county.")
    if not plan.get("stripe_product_id"):
        raise BillingError("This county has no Stripe product set up yet - that has to exist before its price can change.")

    old_price_id = plan.get("stripe_price_id")
    new_price = _stripe_request("POST", "prices", {
        "product": plan["stripe_product_id"],
        "unit_amount": monthly_price_cents,
        "currency": "usd",
        "recurring[interval]": "month",
    })
    pipeline_db.set_pricing_plan_stripe_ids(kind, key, plan["stripe_product_id"], new_price["id"])
    pipeline_db.update_pricing_plan_price(kind, key, monthly_price_cents)

    if old_price_id:
        try:
            _stripe_request("POST", f"prices/{old_price_id}", {"active": "false"})
        except BillingError as e:
            print(f"WARNING: failed to archive old Stripe price {old_price_id} after replacing it: {e}")


# Lob's base rate for the letter type this platform sends (standard,
# black & white, single page, First Class) is $0.658/letter - charged per
# letter with a 10% markup, on top of the flat monthly county subscription.
# The county's letter feature is optional; this is what it costs when
# actually used.
LOB_LETTER_COST_CENTS = 66  # $0.658/letter, rounded to the nearest cent
LETTER_PRICE_CENTS = round(LOB_LETTER_COST_CENTS * 1.10)  # $0.73/letter


def _get_default_payment_method(customer_id):
    """The payment method already on file for this customer from their
    subscription checkout - reused for one-off per-letter charges instead
    of asking them to enter a card again."""
    customer = _stripe_request("GET", f"customers/{customer_id}")
    payment_method = (customer.get("invoice_settings") or {}).get("default_payment_method")
    return payment_method or customer.get("default_source")


def charge_for_letter(customer_id, description):
    """One-off charge for sending a single letter, billed to the same card
    the agent's subscription already uses - separate from that recurring
    subscription charge. Raises BillingError if there's no payment method
    on file or the charge is declined; callers must not send the letter
    through Lob if this raises, so a failed charge never mails for free."""
    payment_method = _get_default_payment_method(customer_id)
    if not payment_method:
        raise BillingError("No payment method on file for this account.")
    return _stripe_request("POST", "payment_intents", {
        "amount": LETTER_PRICE_CENTS,
        "currency": "usd",
        "customer": customer_id,
        "payment_method": payment_method,
        "off_session": "true",
        "confirm": "true",
        "description": description,
    })


def refund_letter_charge(payment_intent_id):
    """Best-effort refund of a per-letter charge whose letter then failed to
    actually send via Lob - called after the fact, so a failure here is
    logged rather than raised (the caller already has a 502 to return for
    the real problem, the failed send, and refunding is a side effect of
    that, not the thing the request is failing on)."""
    try:
        _stripe_request("POST", "refunds", {"payment_intent": payment_intent_id})
    except BillingError as e:
        print(f"WARNING: failed to refund letter charge {payment_intent_id} after a failed send: {e}")


def cancel_and_refund_losing_subscription(subscription_id, reason=""):
    """The losing side of a race for the same county (see
    handle_webhook_event's checkout.session.completed branch) used to just
    print a warning asking a human to cancel/refund it manually - meaning
    the customer kept paying monthly for a county they never got access to,
    for however long it took someone to notice the log line. This cancels
    the subscription immediately and refunds its first invoice, same as
    refund_letter_charge: best-effort and logged rather than raised, since
    the caller's own request (the webhook delivery) already succeeded and
    this is a side effect of that, not the thing that should fail it."""
    try:
        subscription = _stripe_request("GET", f"subscriptions/{subscription_id}")
        invoice_id = subscription.get("latest_invoice")
        _stripe_request("DELETE", f"subscriptions/{subscription_id}")
        if invoice_id:
            invoice = _stripe_request("GET", f"invoices/{invoice_id}")
            payment_intent_id = invoice.get("payment_intent")
            if payment_intent_id:
                _stripe_request("POST", "refunds", {"payment_intent": payment_intent_id})
    except BillingError as e:
        print(f"WARNING: failed to auto-cancel/refund losing subscription {subscription_id} ({reason}): {e} - cancel/refund it manually in Stripe.")


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

        # A brand-new signup's account and mailing profile are now created
        # synchronously in dashboard.py's _handle_signup, before checkout
        # even starts (see that function's docstring) - so there is nothing
        # left to do here for them. This branch used to create the account
        # here instead, keyed off a token in an in-memory dict that a
        # Render restart between checkout and this webhook would wipe -
        # silently losing a paid signup, with no login ever created and the
        # county still locked to that email below. Fixed 2026-09-22.

        if email and kind == "county" and key and subscription_id:
            # Belt-and-suspenders re-check: the /api endpoints already block
            # buying a taken county, but two checkouts for the same county
            # could still race each other between session creation and this
            # webhook firing. Whoever's webhook lands first wins the county;
            # the loser's payment is recorded but access is withheld, and
            # this print is the (deliberately manual) signal to go cancel/
            # refund it in Stripe.
            existing_owner = pipeline_db.get_county_owner(key)
            if existing_owner and existing_owner != email.strip().lower():
                print(
                    f"County '{key}' was already owned by {existing_owner}; "
                    f"{email}'s payment (subscription {subscription_id}) was not granted "
                    f"access - auto-cancelling and refunding that subscription."
                )
                pipeline_db.record_subscription(email, kind, key, customer_id, subscription_id, "conflict")
                cancel_and_refund_losing_subscription(subscription_id, reason=f"lost county race for '{key}'")
            else:
                pipeline_db.record_subscription(email, kind, key, customer_id, subscription_id, "active")
                pipeline_db.grant_county_access(email, key)

    elif event_type == "customer.subscription.deleted":
        subscription_id = obj["id"]
        record = pipeline_db.get_subscription_by_stripe_id(subscription_id)
        if record:
            pipeline_db.update_subscription_status(subscription_id, "canceled")
            if record["kind"] == "county":
                pipeline_db.revoke_county_access(record["email"], record["key"])
                print(f"County '{record['key']}' is now available again (previously held by {record['email']}).")
            elif record["kind"] == "state_bundle":
                pipeline_db.revoke_state_access(record["email"], record["key"])

    elif event_type == "customer.subscription.updated":
        # Previously unhandled entirely: a failing card doesn't necessarily
        # delete the subscription (that depends on the Stripe account's
        # dunning/retry settings) - Stripe can instead leave it "past_due"
        # indefinitely or move it to "unpaid", neither of which ever fires
        # customer.subscription.deleted above, so access was never revoked
        # no matter how long a card kept failing. Reacting to the status
        # Stripe actually reports (rather than only to outright deletion)
        # closes that gap without this code guessing at a grace period -
        # that policy lives in the Stripe account's dunning settings.
        subscription_id = obj["id"]
        new_status = obj.get("status")
        record = pipeline_db.get_subscription_by_stripe_id(subscription_id)
        if record:
            pipeline_db.update_subscription_status(subscription_id, new_status)
            is_county = record["kind"] == "county"
            revoke = pipeline_db.revoke_county_access if is_county else pipeline_db.revoke_state_access
            grant = pipeline_db.grant_county_access if is_county else pipeline_db.grant_state_access
            if new_status in ("unpaid", "canceled", "incomplete_expired"):
                revoke(record["email"], record["key"])
                print(f"{record['kind']} '{record['key']}' access revoked - subscription {subscription_id} is now '{new_status}' (previously held by {record['email']}).")
            elif new_status == "active":
                # Recovered - e.g. the customer updated a card that had been
                # failing. Re-grant in case an earlier update on this same
                # subscription had revoked it.
                grant(record["email"], record["key"])

    elif event_type == "invoice.payment_failed":
        # No action taken here - Stripe's own retry schedule and dunning
        # settings decide what happens next, and customer.subscription.
        # updated above reacts once the subscription's status actually
        # changes. This is just making the failure visible at all, which it
        # previously was not: the event reached this handler and nothing
        # happened, silently, every time.
        print(f"NOTE: invoice payment failed for subscription {obj.get('subscription')} (customer {obj.get('customer')}).")

    return event_type
