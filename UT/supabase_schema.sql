-- Run this once in the Supabase SQL Editor (project: sxfqpxevmaajlxnkiobj)
-- to create the tables UT/pipeline_db.py reads/writes.

create table if not exists pdf_templates (
    id bigint generated always as identity primary key,
    company_name text not null,
    template_name text not null,
    filename text not null,
    field_positions jsonb not null default '{}'::jsonb,
    uploaded_at timestamptz not null default now(),
    owner_email text
);

-- 2026-09-22 security fix: this table originally had no owner column, so
-- every signed-in account could list, download, reposition, delete and
-- mail with every other account's templates. `create table if not exists`
-- above is a no-op against an already-existing table, so this ALTER runs
-- the actual migration - safe to re-run, and a no-op once the column
-- exists. Existing rows get owner_email = NULL, which UT/pipeline_db.py's
-- get_pdf_templates() treats as admin-only, not "everyone's" - reassign
-- any pre-existing template you want a specific customer to keep using by
-- setting its owner_email to that customer's account email.
alter table pdf_templates add column if not exists owner_email text;

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
    sent_at timestamptz not null default now(),
    sender_email text
);

-- 2026-09-22 accountability fix: previously nothing recorded who actually
-- clicked Send - only a stdout print when an admin sent on an agent's
-- behalf, unsearchable and gone on restart. See the ALTER below for an
-- already-existing table.
alter table letters_sent add column if not exists sender_email text;

-- 2026-09-22 accountability fix: a durable trail of who did a consequential
-- action - template deletion, a price change, a county status change,
-- granting access - none of which left any record before. See
-- pipeline_db.record_audit_event().
create table if not exists audit_log (
    id bigint generated always as identity primary key,
    actor_email text,
    action text not null,
    target text,
    details text,
    created_at timestamptz not null default now()
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

-- Added 2026-08-04 (invite-only signup): the public signup form only creates
-- an account if the email is listed here first. Add rows by hand in the
-- Supabase Table Editor, same pattern already used for admin-added accounts.
-- redeemed_at is set once the invite is used - purely for visibility, since
-- Supabase Auth's own unique-email constraint is what actually stops reuse.
create table if not exists invited_emails (
    id bigint generated always as identity primary key,
    email text not null unique,
    invited_at timestamptz not null default now(),
    redeemed_at timestamptz
);

-- Added 2026-08-26 (The Map, admin-only pipeline-progress page): manually
-- flags a US county as "being built" before it has a real pipeline folder
-- (which is what "ready to sell" and normally "sold" are computed from
-- instead, in dashboard.py's COUNTY_FOLDER_TO_FIPS). A county absent from
-- this table is "not started". The customer_* columns are only used for a
-- 'sold' row - recording a sale closed outside the self-serve Stripe
-- checkout flow (see handle_set_county_status), for a county the map shows
-- as "ready to sell" (yellow) turning "sold" (green). A real Stripe owner
-- always takes priority over a manually-recorded sale.
create table if not exists county_manual_status (
    county_fips text primary key,
    status text not null,
    customer_name text,
    customer_email text,
    customer_phone text,
    notes text,
    updated_at timestamptz not null default now()
);
