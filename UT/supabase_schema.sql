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
