-- Run this once in the Supabase SQL Editor (project: sxfqpxevmaajlxnkiobj)
-- to create the tables UT/pipeline_db.py reads/writes.

create table if not exists pdf_templates (
    id bigint generated always as identity primary key,
    company_name text not null,
    template_name text not null,
    filename text not null,
    field_positions jsonb not null default '{}'::jsonb,
    uploaded_at timestamptz not null default now()
);

create table if not exists letters_sent (
    id bigint generated always as identity primary key,
    county text,
    full_name text not null,
    owner_name text,
    parcel_id text,
    template_id text,
    message text,
    lob_letter_id text,
    status text,
    expected_delivery_date text,
    preview_url text,
    test_mode boolean not null default true,
    error text,
    sent_at timestamptz not null default now()
);

-- Added 2026-07-27: which counties each logged-in user (by email) is allowed
-- to see/send letters for. A user with zero rows here has no county access
-- at all - access must be explicitly granted, nothing is open by default.
create table if not exists user_county_access (
    id bigint generated always as identity primary key,
    email text not null,
    county text not null,
    unique (email, county)
);

-- Added 2026-07-27 (Stripe billing): a state-wide grant, separate from
-- single-county grants above. Buying the whole-state bundle grants every
-- county in that state, including ones added after the purchase - so this
-- stores the state itself, not a snapshot of whichever counties exist today.
create table if not exists user_state_access (
    id bigint generated always as identity primary key,
    email text not null,
    state text not null,
    unique (email, state)
);

-- What each plan costs and its corresponding Stripe Product/Price. kind is
-- 'county' (key = county folder like 'slk') or 'state_bundle' (key = state
-- abbreviation like 'UT').
create table if not exists pricing_plans (
    id bigint generated always as identity primary key,
    kind text not null check (kind in ('county', 'state_bundle')),
    key text not null,
    label text not null,
    monthly_price_cents integer not null,
    stripe_product_id text,
    stripe_price_id text,
    unique (kind, key)
);

-- One row per Stripe subscription, so the webhook handler can look up what
-- to grant/revoke from just the Stripe subscription/customer IDs an event
-- carries - Stripe's webhook payloads don't include our own email/plan info
-- unless we've recorded the mapping ourselves.
create table if not exists subscriptions (
    id bigint generated always as identity primary key,
    email text not null,
    kind text not null check (kind in ('county', 'state_bundle')),
    key text not null,
    stripe_customer_id text not null,
    stripe_subscription_id text not null unique,
    status text not null,
    created_at timestamptz not null default now()
);

-- Added 2026-07-27 (public signup): each agent's own return address for
-- outgoing letters, collected at signup. mailer.py falls back to the
-- LOB_FROM_* env vars (the original Summit Sotheby's identity) when an
-- email has no row here, so the original admin account is unaffected.
create table if not exists user_mailing_profile (
    id bigint generated always as identity primary key,
    email text not null unique,
    from_name text not null,
    from_address1 text not null,
    from_address2 text,
    from_city text not null,
    from_state text not null,
    from_zip text not null
);
