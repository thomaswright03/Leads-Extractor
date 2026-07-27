"""Sends physical letters via the Lob API (https://docs.lob.com).

Uses only the standard library (urllib) so this doesn't add a new dependency
on top of the existing pandas requirement. Reads all credentials/config from
environment variables - nothing here is ever hardcoded:

  LOB_API_KEY        required. Test key (test_...) or live key (live_...).
  LOB_FROM_NAME       required. Return-address name printed on the letter.
  LOB_FROM_ADDRESS1   required. Return-address street line.
  LOB_FROM_ADDRESS2   optional. Suite/unit line.
  LOB_FROM_CITY       required.
  LOB_FROM_STATE      required. Two-letter state code.
  LOB_FROM_ZIP        required.
"""

import base64
import json
import os
import urllib.error
import urllib.request
import uuid

LOB_API_URL = "https://api.lob.com/v1/letters"


class MailerConfigError(Exception):
    """Raised when required env vars aren't set."""


class LobAPIError(Exception):
    """Raised when Lob rejects the request - message is Lob's own error text."""


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise MailerConfigError(
            f"{name} is not set. Export it in your shell (e.g. ~/.zshrc) before sending letters."
        )
    return value


def is_test_mode():
    """Lob's own convention: test key ids are prefixed 'test_', live ones 'live_'."""
    return os.environ.get("LOB_API_KEY", "").strip().startswith("test_")


def _from_address():
    address = {
        "name": _require_env("LOB_FROM_NAME"),
        "address_line1": _require_env("LOB_FROM_ADDRESS1"),
        "address_city": _require_env("LOB_FROM_CITY"),
        "address_state": _require_env("LOB_FROM_STATE"),
        "address_zip": _require_env("LOB_FROM_ZIP"),
    }
    address_line2 = os.environ.get("LOB_FROM_ADDRESS2", "").strip()
    if address_line2:
        address["address_line2"] = address_line2
    return address


def _run_request(request):
    """Shared by send_letter/send_letter_pdf: executes the request and turns
    any failure into a LobAPIError carrying Lob's own error message."""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(error_body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = error_body or str(e)
        raise LobAPIError(message) from e
    except urllib.error.URLError as e:
        raise LobAPIError(f"Couldn't reach Lob: {e.reason}") from e


def send_letter(to_name, address_line1, city, state, zip_code, html_body, description="", from_address=None):
    """Creates a letter via POST /v1/letters. Returns Lob's response dict
    (includes 'id', 'expected_delivery_date', 'url' to a preview PDF) on
    success. Raises LobAPIError with Lob's own error message on failure.

    from_address lets a caller send on behalf of a specific agent (their own
    return address, looked up by dashboard.py); omitting it falls back to
    the LOB_FROM_* env vars, unchanged from before multi-tenant support."""
    api_key = _require_env("LOB_API_KEY")
    from_address = from_address or _from_address()

    payload = {
        "description": description or f"Letter to {to_name}",
        "to": {
            "name": to_name,
            "address_line1": address_line1,
            "address_city": city,
            "address_state": state,
            "address_zip": zip_code,
        },
        "from": from_address,
        "file": html_body,
        "color": False,
        "use_type": "operational",
    }

    body = json.dumps(payload).encode("utf-8")
    auth = base64.b64encode(f"{api_key}:".encode("ascii")).decode("ascii")
    request = urllib.request.Request(
        LOB_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
    )
    return _run_request(request)


def _encode_multipart(fields, file_field_name, filename, file_bytes, file_content_type):
    """Minimal multipart/form-data encoder (stdlib only). Lob's `file` field
    only accepts raw PDF bytes over multipart, not JSON, so a pre-designed
    PDF has to go out this way instead of through send_letter()'s JSON body."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    parts.append(f"--{boundary}".encode())
    parts.append(f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"'.encode())
    parts.append(f"Content-Type: {file_content_type}".encode())
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    return b"\r\n".join(parts), f"multipart/form-data; boundary={boundary}"


def send_letter_pdf(to_name, address_line1, city, state, zip_code, pdf_bytes, description="", from_address=None):
    """Same as send_letter() but the letter content is an already-designed
    PDF (uploaded by an outside company) sent to Lob as-is, rather than HTML
    generated from a merge-field message. See send_letter() for from_address."""
    api_key = _require_env("LOB_API_KEY")
    from_address = from_address or _from_address()

    fields = {
        "description": description or f"Letter to {to_name}",
        "to[name]": to_name,
        "to[address_line1]": address_line1,
        "to[address_city]": city,
        "to[address_state]": state,
        "to[address_zip]": zip_code,
        "from[name]": from_address["name"],
        "from[address_line1]": from_address["address_line1"],
        "from[address_city]": from_address["address_city"],
        "from[address_state]": from_address["address_state"],
        "from[address_zip]": from_address["address_zip"],
        "color": "false",
        "use_type": "operational",
    }
    if "address_line2" in from_address:
        fields["from[address_line2]"] = from_address["address_line2"]

    body, content_type = _encode_multipart(fields, "file", "letter.pdf", pdf_bytes, "application/pdf")
    auth = base64.b64encode(f"{api_key}:".encode("ascii")).decode("ascii")
    request = urllib.request.Request(
        LOB_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": content_type,
        },
    )
    return _run_request(request)
