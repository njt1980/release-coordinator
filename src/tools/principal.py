"""The signed-in user, as Azure Container Apps / App Service authentication passes it.

Easy Auth sets ``X-MS-CLIENT-PRINCIPAL-NAME`` (the user principal name),
``X-MS-CLIENT-PRINCIPAL-ID`` (the Entra object ID) and
``X-MS-CLIENT-PRINCIPAL`` (base64 JSON with the token's claims, including
the display name and app roles), and strips client-supplied copies. These
headers are trusted only when ``RC_TRUST_PRINCIPAL_HEADER`` says the server
runs behind it.
"""

import base64
import binascii
import json
from typing import Final

from pydantic import BaseModel, Field

NAME_HEADER: Final[str] = "X-MS-CLIENT-PRINCIPAL-NAME"
ID_HEADER: Final[str] = "X-MS-CLIENT-PRINCIPAL-ID"
CLAIMS_HEADER: Final[str] = "X-MS-CLIENT-PRINCIPAL"
_ROLE_CLAIMS: Final[frozenset[str]] = frozenset(
    {"roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}
)
_DISPLAY_NAME_CLAIM: Final[str] = "name"


class Principal(BaseModel):
    """A signed-in user: printed name, user principal name, object ID, app roles."""

    display_name: str
    user_name: str = Field(min_length=1)
    object_id: str | None = None
    roles: frozenset[str] = frozenset()


def _claims(encoded: str | None) -> list[tuple[str, str]]:
    """The (type, value) claims of an ``X-MS-CLIENT-PRINCIPAL`` header; [] when unreadable."""
    if not encoded:
        return []
    try:
        payload = json.loads(base64.b64decode(encoded, validate=False))
    except (binascii.Error, ValueError):
        return []
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        return []
    pairs: list[tuple[str, str]] = []
    for claim in claims:
        if isinstance(claim, dict) and isinstance(claim.get("typ"), str) and isinstance(claim.get("val"), str):
            pairs.append((claim["typ"], claim["val"]))
    role_type = payload.get("role_typ")
    if isinstance(role_type, str):
        pairs = [("roles", value) if kind == role_type else (kind, value) for kind, value in pairs]
    return pairs


def parse_principal(
    user_name: str | None, object_id: str | None, encoded_claims: str | None
) -> Principal | None:
    """The signed-in user from Easy Auth's headers, or None when no user name was passed."""
    if not user_name or not user_name.strip():
        return None
    claims = _claims(encoded_claims)
    display = next((value for kind, value in claims if kind == _DISPLAY_NAME_CLAIM and value.strip()), "")
    return Principal(
        display_name=display.strip() or user_name.strip(),
        user_name=user_name.strip(),
        object_id=(object_id or "").strip() or None,
        roles=frozenset(value for kind, value in claims if kind in _ROLE_CLAIMS),
    )
