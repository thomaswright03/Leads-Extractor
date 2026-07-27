"""Real login backed by Supabase Auth, replacing the old hardcoded
client-side email/password check. Account creation is intentionally NOT
handled here - per the user's choice, new users are added directly in the
Supabase dashboard (Authentication -> Users -> Add user, with "Auto Confirm
User" checked so no email-confirmation step is needed), not through a public
sign-up form in this app.

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
