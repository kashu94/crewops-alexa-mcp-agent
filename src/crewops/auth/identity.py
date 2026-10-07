"""A signed-in user, from their Cognito token's claims, as the ``Caller`` the tools check.

The claims must come from a token whose signature has already been verified, by whatever
receives the request (AgentCore's JWT authorizer, in front of the MCP server): this module
does not check signatures. It is the second line: only an access token is accepted (an id
token is for the client, not for calling services), and it must name this pool, this app
client and be current. Then the roles come from the groups, which only an admin can change,
and the crew id and desk from the user's profile in the operations table, which only the
team command writes. Nothing comes from what a user typed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from crewops.domain import Role
from crewops.tools import Caller


class AuthError(Exception):
    """The claims do not identify a usable account. Refuse the request."""


def issuer_for(region: str, user_pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def _groups(value: Any) -> list[str]:
    """``cognito:groups`` is a list in the token; a front end may pass it on as a JSON list
    or as space-separated text. Anything else holds no groups."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = text.strip("[]").split()
            return [v for v in parsed if isinstance(v, str)]
        return text.split()
    return []


def caller_from_claims(
    claims: Mapping[str, Any],
    profiles: Callable[[str], Mapping[str, Any] | None],
    *,
    issuer: str,
    client_id: str,
    now: float | None = None,
) -> Caller:
    """The ``Caller`` for a signature-verified access token's claims, or ``AuthError``."""
    if not issuer or not client_id:
        raise AuthError("the expected issuer and app client must be given")
    if claims.get("token_use") != "access":
        raise AuthError("not a Cognito access token")
    if claims.get("iss") != issuer:
        raise AuthError("token is from another user pool")
    if claims.get("client_id") != client_id:
        raise AuthError("token is for another app client")
    expires = claims.get("exp")
    if not isinstance(expires, int | float) or expires <= (time.time() if now is None else now):
        raise AuthError("token has expired")

    username = claims.get("username")
    if not isinstance(username, str) or not username:
        raise AuthError("token names no user")
    username = username.lower()  # sign-in is case-insensitive; profiles are stored lower-case
    roles = frozenset(Role(g) for g in _groups(claims.get("cognito:groups")) if g in Role)
    if not roles:
        raise AuthError(f"{username} holds no role")

    profile = profiles(username) or {}
    crew_id = profile.get("crew_id") if Role.CREW in roles else None
    desk = profile.get("desk") if Role.CONTROLLER in roles else None
    if Role.CREW in roles and not crew_id:
        raise AuthError(f"{username} is crew but is linked to no crew record")
    if Role.CONTROLLER in roles and not desk:
        raise AuthError(f"{username} is a controller but holds no desk")
    return Caller(username, roles, crew_id, desk)
