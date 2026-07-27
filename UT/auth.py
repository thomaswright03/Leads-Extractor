"""Real login backed by Supabase Auth, replacing the old hardcoded
client-side email/password check.

Account creation has two paths: admin-added accounts (created directly in
the Supabase dashboard, Authentication -> Users -> Add user - unaffected,
still just log in) and the public paid sign-up flow added 2026-07-27
(sign_up() below), which auto-confirms the account server-side since
there's no email/SMTP setup to send a confirmation link through.

Reads SUPABASE_URL and SUPABASE_SECRET_KEY from the environment - same
credentials pipeline_db.py already uses.
"""
import os

from supabase import create_client
from supabase_auth.errors import AuthApiError

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


class LoginError(Exception):
    """User-facing message - invalid credentials or a Supabase-side error."""


def sign_in(email, password):
    """Returns (email, expires_at_unix_ts) on success. Raises LoginError otherwise."""
    try:
        result = _get_client().auth.sign_in_with_password({"email": email, "password": password})
    except AuthApiError as e:
        raise LoginError(str(e)) from e
    return result.user.email, result.session.expires_at


class SignupError(Exception):
    """User-facing message - duplicate email, weak password, or a
    Supabase-side error."""


def sign_up(email, password):
    """Creates a new Supabase Auth user, auto-confirmed (no email step) -
    the public paid sign-up path. Returns the created user's email. Raises
    SignupError on failure (most commonly: that email is already registered)."""
    try:
        result = _get_client().auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
    except AuthApiError as e:
        raise SignupError(str(e)) from e
    return result.user.email
