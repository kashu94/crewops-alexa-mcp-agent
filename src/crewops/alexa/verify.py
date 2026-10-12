"""Check that a request really comes from Alexa, as Amazon requires of a skill hosted on
an HTTPS endpoint instead of an Alexa-triggered Lambda.

1. ``SignatureCertChainUrl`` is Amazon's: https, ``s3.amazonaws.com``, port 443, a path
   under ``/echo.api/``.
2. The certificate chain at that URL is current, leads to a trusted root, and names
   ``echo-api.amazon.com``.
3. ``Signature-256`` is the chain's SHA-256 RSA signature of the exact request body.
4. The request was made within the last 150 seconds.
5. It is for this skill.

Anything else is refused before the body is acted on. Chains are cached by URL.
"""

from __future__ import annotations

import base64
import json
import posixpath
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

TOLERANCE_SECONDS = 150
ECHO_NAME = "echo-api.amazon.com"


class NotFromAlexa(Exception):
    """The request failed a check: answer 400 and do nothing."""


def normalised_cert_url(url: str) -> str | None:
    """The certificate URL with dot segments and duplicate slashes removed, if it is
    Amazon's; None otherwise. A query string is refused: Amazon's URL has none, and each
    new one would otherwise mean another fetch and another cached chain."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    path = posixpath.normpath(parsed.path) if parsed.path else ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "s3.amazonaws.com"
        or port not in (None, 443)
        or parsed.query
        or not path.startswith("/echo.api/")
    ):
        return None
    return f"https://s3.amazonaws.com{path}"


def valid_cert_url(url: str) -> bool:
    return normalised_cert_url(url) is not None


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (an https URL checked above)
        return r.read()


_chains: dict[str, Any] = {}
MAX_CHAINS = 8
"""Amazon rotates its signing certificate rarely: a handful of cached chains is plenty."""


def _chain(url: str, fetch: Callable[[str], bytes]) -> list[Any]:
    from cryptography import x509

    if url not in _chains:
        try:
            chain = x509.load_pem_x509_certificates(fetch(url))
        except Exception as exc:  # unreachable, or not a certificate chain
            raise NotFromAlexa(f"certificate chain unavailable: {type(exc).__name__}") from None
        if len(_chains) >= MAX_CHAINS:
            _chains.pop(next(iter(_chains)))
        _chains[url] = chain
    return _chains[url]


def _trusted_store() -> Any:
    import certifi
    from cryptography import x509
    from cryptography.x509.verification import Store

    with open(certifi.where(), "rb") as f:
        return Store(x509.load_pem_x509_certificates(f.read()))


def check_chain(chain: list[Any], store: Any = None, now: datetime | None = None) -> Any:
    """The signing certificate, once its chain is verified for echo-api.amazon.com."""
    from cryptography import x509
    from cryptography.x509.verification import PolicyBuilder, VerificationError

    if not chain:
        raise NotFromAlexa("empty certificate chain")
    leaf, intermediates = chain[0], chain[1:]
    verifier = (
        PolicyBuilder()
        .store(store or _trusted_store())
        .time(now or datetime.now(UTC))
        .build_server_verifier(x509.DNSName(ECHO_NAME))
    )
    try:
        verifier.verify(leaf, intermediates)
    except VerificationError as exc:
        raise NotFromAlexa(f"certificate chain refused: {exc}") from None
    return leaf


def check_signature(leaf: Any, body: bytes, signature_b64: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        leaf.public_key().verify(
            base64.b64decode(signature_b64), body, padding.PKCS1v15(), hashes.SHA256()
        )
    except (InvalidSignature, ValueError) as exc:
        raise NotFromAlexa("signature does not match the body") from exc


def check_timestamp(envelope: dict[str, Any], now: float | None = None) -> None:
    stamp = (envelope.get("request") or {}).get("timestamp")
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise NotFromAlexa("request has no valid timestamp") from None
    if abs((time.time() if now is None else now) - at) > TOLERANCE_SECONDS:
        raise NotFromAlexa("request is too old or from the future")


def application_id(envelope: dict[str, Any]) -> str | None:
    system = (envelope.get("context") or {}).get("System") or {}
    app = (system.get("application") or {}).get("applicationId")
    return app or ((envelope.get("session") or {}).get("application") or {}).get("applicationId")


def verified(
    body: bytes,
    headers: dict[str, str],
    skill_id: str,
    *,
    fetch: Callable[[str], bytes] = _fetch,
    store: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The request envelope, once every check passes; ``NotFromAlexa`` otherwise."""
    lower = {k.lower(): v for k, v in headers.items()}
    url = lower.get("signaturecertchainurl", "")
    signature = lower.get("signature-256", "")
    if not url or not signature:
        raise NotFromAlexa("missing signature headers")
    clean = normalised_cert_url(url)
    if clean is None:
        raise NotFromAlexa("certificate URL is not Amazon's")
    leaf = check_chain(_chain(clean, fetch), store, now)
    check_signature(leaf, body, signature)
    try:
        envelope = json.loads(body)
    except ValueError:
        raise NotFromAlexa("body is not JSON") from None
    check_timestamp(envelope, now.timestamp() if now else None)
    if not skill_id or application_id(envelope) != skill_id:
        raise NotFromAlexa("request is for another skill")
    return envelope
