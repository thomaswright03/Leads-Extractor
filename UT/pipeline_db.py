"""Central mailing-state store shared by every county's confirmed-leads view
in dashboard.py - deliberately separate from each county's own
pipeline_cache.db (SLKobi.py/SLK$.py/DC.py/pricer.py's obituary-scraping and
matching state, which stays put per county folder, still SQLite). PDF
templates and send history are the same across counties (one business, one
set of letter designs, one Lob account), so they live once in Supabase
instead of duplicated per county.

Reads SUPABASE_URL and SUPABASE_SECRET_KEY from the environment - never
hardcoded. SUPABASE_SECRET_KEY must be the service_role secret key (not the
publishable/anon key): this is a trusted backend script, and service_role is
what lets it read/write regardless of Row Level Security policies.

Requires the `pdf_templates` and `letters_sent` tables to already exist -
see supabase_schema.sql in this same folder; run it once in the Supabase
SQL Editor before this module is used.
"""
import os
from datetime import datetime, timezone

from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

_client = None


def _get_client():
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must be set in the environment "
                "(export them in ~/.zshrc, same pattern as LOB_API_KEY)."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def _row_to_pdf_template(row):
    return {
        "id": row["id"],
        "companyName": row["company_name"],
        "templateName": row["template_name"],
        "filename": row["filename"],
        "fieldPositions": row.get("field_positions") or {},
        "uploadedAt": row.get("uploaded_at"),
    }


def add_pdf_template(company_name, template_name, filename):
    """filename is the on-disk name under dashboard.py's pdf_templates/ dir,
    not a path - the caller owns where the actual bytes live (still local
    disk; only the metadata/history in this module moved to Supabase)."""
    result = (
        _get_client()
        .table("pdf_templates")
        .insert({"company_name": company_name, "template_name": template_name, "filename": filename})
        .execute()
    )
    return result.data[0]["id"]


def get_pdf_templates():
    result = _get_client().table("pdf_templates").select("*").order("id", desc=True).execute()
    return [_row_to_pdf_template(r) for r in result.data]


def get_pdf_template(template_id):
    result = _get_client().table("pdf_templates").select("*").eq("id", template_id).execute()
    return _row_to_pdf_template(result.data[0]) if result.data else None


def set_pdf_template_field_position(template_id, field, x_ratio, y_ratio):
    """Records where on the page (as a 0-1 ratio of width/height, not
    absolute points) a merge field should be stamped for this PDF template.
    Ratio-based so it's independent of whatever size the click-to-place
    preview image was rendered at."""
    template = get_pdf_template(template_id)
    if not template:
        return False
    positions = template["fieldPositions"]
    positions[field] = {"x": x_ratio, "y": y_ratio}
    _get_client().table("pdf_templates").update({"field_positions": positions}).eq("id", template_id).execute()
    return True


def delete_pdf_template(template_id):
    _get_client().table("pdf_templates").delete().eq("id", template_id).execute()


def record_letter_sent(county, full_name, owner_name, parcel_id, template_id, message,
                        lob_letter_id="", status="", expected_delivery_date="",
                        preview_url="", test_mode=True, error=""):
    """One row per send attempt - letters_sent is never wiped, so this is the
    durable record of what was mailed. Keyed for lookups by
    (county, full_name, parcel_id) since parcel numbering isn't guaranteed
    unique across different counties' assessor systems."""
    _get_client().table("letters_sent").insert({
        "county": county,
        "full_name": full_name,
        "owner_name": owner_name,
        "parcel_id": parcel_id,
        "template_id": template_id,
        "message": message,
        "lob_letter_id": lob_letter_id,
        "status": status,
        "expected_delivery_date": expected_delivery_date,
        "preview_url": preview_url,
        "test_mode": bool(test_mode),
        "error": error,
    }).execute()


def get_counties_for_user(email):
    """County folder keys (e.g. "slk", "uta") this email is allowed to see -
    empty list means no access at all. Access is opt-in only: a user with no
    rows in user_county_access sees nothing, nothing is open by default."""
    if not email:
        return []
    result = (
        _get_client()
        .table("user_county_access")
        .select("county")
        .eq("email", email.strip().lower())
        .execute()
    )
    return [row["county"] for row in result.data]


def grant_county_access(email, county):
    # upsert, not insert: Stripe webhooks can redeliver the same event, and
    # a plain insert would crash on the (email, county) unique constraint
    # the second time.
    _get_client().table("user_county_access").upsert(
        {"email": email.strip().lower(), "county": county}, on_conflict="email,county"
    ).execute()


def revoke_county_access(email, county):
    _get_client().table("user_county_access").delete().eq(
        "email", email.strip().lower()
    ).eq("county", county).execute()


def get_taken_counties():
    """Folder keys of every county that currently has an owner. Under the
    one-agent-per-county model, a county in this set can't be sold to
    anyone else until its current owner's access is revoked."""
    result = _get_client().table("user_county_access").select("county").execute()
    return {row["county"] for row in result.data}


def get_county_owner(county):
    """Email of the agent currently holding exclusive access to this
    county, or None if it's unclaimed."""
    result = (
        _get_client()
        .table("user_county_access")
        .select("email")
        .eq("county", county)
        .limit(1)
        .execute()
    )
    return result.data[0]["email"] if result.data else None


def get_active_customer_id(kind, key):
    """The Stripe customer id behind the currently-active subscription for
    this plan, or None (e.g. an admin testing an unclaimed county, or an
    account granted access by hand rather than through checkout) - used to
    bill one-off per-letter charges to the same card the subscription
    already uses."""
    result = (
        _get_client()
        .table("subscriptions")
        .select("stripe_customer_id")
        .eq("kind", kind)
        .eq("key", key)
        .eq("status", "active")
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0]["stripe_customer_id"] if result.data else None


def get_state_grants_for_user(email):
    """State abbreviations (e.g. "UT") this email has whole-state access to,
    from buying the state bundle rather than individual counties. The caller
    (dashboard.py, which owns the COUNTIES list) is responsible for expanding
    a state grant into the actual counties currently in that state - storing
    the state itself here, not a snapshot of counties, is what makes a
    state-bundle subscriber automatically get access to a county added later."""
    if not email:
        return []
    result = (
        _get_client()
        .table("user_state_access")
        .select("state")
        .eq("email", email.strip().lower())
        .execute()
    )
    return [row["state"] for row in result.data]


def grant_state_access(email, state):
    _get_client().table("user_state_access").upsert(
        {"email": email.strip().lower(), "state": state}, on_conflict="email,state"
    ).execute()


def revoke_state_access(email, state):
    _get_client().table("user_state_access").delete().eq(
        "email", email.strip().lower()
    ).eq("state", state).execute()


def get_pricing_plans():
    result = _get_client().table("pricing_plans").select("*").execute()
    return result.data


def get_pricing_plan(kind, key):
    result = (
        _get_client()
        .table("pricing_plans")
        .select("*")
        .eq("kind", kind)
        .eq("key", key)
        .execute()
    )
    return result.data[0] if result.data else None


def create_pricing_plan(kind, key, label, monthly_price_cents):
    result = (
        _get_client()
        .table("pricing_plans")
        .insert({"kind": kind, "key": key, "label": label, "monthly_price_cents": monthly_price_cents})
        .execute()
    )
    return result.data[0]["id"]


def set_pricing_plan_stripe_ids(kind, key, stripe_product_id, stripe_price_id):
    _get_client().table("pricing_plans").update(
        {"stripe_product_id": stripe_product_id, "stripe_price_id": stripe_price_id}
    ).eq("kind", kind).eq("key", key).execute()


def record_subscription(email, kind, key, stripe_customer_id, stripe_subscription_id, status):
    _get_client().table("subscriptions").upsert({
        "email": email.strip().lower(),
        "kind": kind,
        "key": key,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "status": status,
    }, on_conflict="stripe_subscription_id").execute()


def get_subscription_by_stripe_id(stripe_subscription_id):
    result = (
        _get_client()
        .table("subscriptions")
        .select("*")
        .eq("stripe_subscription_id", stripe_subscription_id)
        .execute()
    )
    return result.data[0] if result.data else None


def update_subscription_status(stripe_subscription_id, status):
    _get_client().table("subscriptions").update({"status": status}).eq(
        "stripe_subscription_id", stripe_subscription_id
    ).execute()


def get_mailing_profile(email):
    """This agent's own return address for outgoing letters, or None if
    they've never set one (e.g. the original admin account, which keeps
    using mailer.py's LOB_FROM_* env var fallback instead)."""
    if not email:
        return None
    result = (
        _get_client()
        .table("user_mailing_profile")
        .select("*")
        .eq("email", email.strip().lower())
        .execute()
    )
    return result.data[0] if result.data else None


def set_mailing_profile(email, from_name, from_address1, from_address2, from_city, from_state, from_zip):
    _get_client().table("user_mailing_profile").upsert({
        "email": email.strip().lower(),
        "from_name": from_name,
        "from_address1": from_address1,
        "from_address2": from_address2 or None,
        "from_city": from_city,
        "from_state": from_state,
        "from_zip": from_zip,
    }, on_conflict="email").execute()


def is_email_invited(email):
    """Gate for the public signup form: only emails added by hand to
    invited_emails (Supabase Table Editor) are allowed to create an account."""
    if not email:
        return False
    result = (
        _get_client()
        .table("invited_emails")
        .select("email")
        .eq("email", email.strip().lower())
        .execute()
    )
    return bool(result.data)


def mark_invite_redeemed(email):
    _get_client().table("invited_emails").update(
        {"redeemed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("email", email.strip().lower()).execute()


def get_latest_letters_sent():
    """Latest send attempt per (county, full_name, parcel_id), keyed the same
    way dashboard.py keys its merged, multi-county confirmed-leads list."""
    result = (
        _get_client()
        .table("letters_sent")
        .select("county,full_name,parcel_id,template_id,lob_letter_id,status,"
                "expected_delivery_date,preview_url,test_mode,error,sent_at")
        .order("id")
        .execute()
    )
    latest = {}
    for row in result.data:
        key = (row["county"], row["full_name"], row["parcel_id"])
        latest[key] = {
            "templateId": row["template_id"],
            "lobLetterId": row["lob_letter_id"],
            "status": row["status"],
            "expectedDeliveryDate": row["expected_delivery_date"],
            "previewUrl": row["preview_url"],
            "testMode": bool(row["test_mode"]),
            "error": row["error"],
            "sentAt": row["sent_at"],
        }
    return latest
