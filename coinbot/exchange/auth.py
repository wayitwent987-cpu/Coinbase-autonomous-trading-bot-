"""Coinbase CDP request signing (JWT, ES256 or Ed25519).

Advanced Trade authenticates each request with a short-lived JWT whose ``uri``
claim binds it to one HTTP method and path. That binding is the reason a
captured token cannot be replayed against a different endpoint, so the URI must
be built exactly as Coinbase expects: ``"METHOD host/path"``, no scheme, no
query string.
"""

from __future__ import annotations

import base64
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

try:
    import jwt as pyjwt
except ImportError:  # pragma: no cover
    pyjwt = None  # type: ignore[assignment]

from ..logging_setup import get_logger
from .errors import AuthenticationError

log = get_logger("auth")

_JWT_ALGORITHM_EC = "ES256"
_JWT_ALGORITHM_ED = "EdDSA"


class CoinbaseAuth:
    """Builds the ``Authorization: Bearer <jwt>`` header for each request.

    A fresh JWT is minted per request. They are valid for two minutes, and
    reusing one across endpoints would break the ``uri`` binding, so there is
    nothing worth caching.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        ttl_seconds: int = 120,
    ) -> None:
        if pyjwt is None:  # pragma: no cover
            raise AuthenticationError(
                "PyJWT is required for authenticated requests: pip install PyJWT"
            )
        if not api_key or not api_secret:
            raise AuthenticationError("both api_key (key name) and api_secret are required")

        self.api_key = api_key.strip()
        self.ttl_seconds = ttl_seconds
        self._raw_secret = api_secret
        self._private_key, self.algorithm = self._load_key(api_secret)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_key(secret: str):
        """Accept either an EC private-key PEM or a base64 Ed25519 key.

        Coinbase has issued both formats: older CDP keys are EC (ES256) PEMs,
        newer ones can be raw base64 Ed25519. Supporting both means a user's
        downloaded key file works without them having to know which they have.
        """
        text = secret.strip().replace("\\n", "\n")

        if "BEGIN" in text and "PRIVATE KEY" in text:
            try:
                from cryptography.hazmat.primitives.serialization import load_pem_private_key

                load_pem_private_key(text.encode(), password=None)
            except Exception as exc:  # pragma: no cover - depends on key material
                raise AuthenticationError(f"could not parse EC private key PEM: {exc}") from exc
            return text, _JWT_ALGORITHM_EC

        # Ed25519 keys arrive base64-encoded, 64 bytes (seed + public key).
        try:
            decoded = base64.b64decode(text, validate=True)
        except Exception as exc:
            raise AuthenticationError(
                "api_secret is neither a PEM private key nor valid base64"
            ) from exc

        if len(decoded) not in (32, 64):
            raise AuthenticationError(
                f"unexpected Ed25519 key length {len(decoded)}; expected 32 or 64 bytes"
            )
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            key = Ed25519PrivateKey.from_private_bytes(decoded[:32])
        except Exception as exc:  # pragma: no cover
            raise AuthenticationError(f"could not build Ed25519 key: {exc}") from exc
        return key, _JWT_ALGORITHM_ED

    # ------------------------------------------------------------------ #

    @staticmethod
    def build_uri(method: str, url: str) -> str:
        """``"GET https://api.coinbase.com/x?y=1"`` -> ``"GET api.coinbase.com/x"``.

        The query string is excluded on purpose: Coinbase signs only the path,
        and including it produces a 401 that is genuinely hard to diagnose.
        """
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path.split("/")[0]
        path = parsed.path if parsed.netloc else "/" + "/".join(parsed.path.split("/")[1:])
        return f"{method.upper()} {host}{path}"

    def token(self, method: str, url: str) -> str:
        """Mint a JWT bound to this method and path."""
        now = int(time.time())
        uri = self.build_uri(method, url)
        payload = {
            "sub": self.api_key,
            "iss": "cdp",
            "nbf": now,
            "exp": now + self.ttl_seconds,
            "uri": uri,
        }
        headers = {
            "kid": self.api_key,
            # A per-request nonce keeps two tokens minted in the same second
            # from being byte-identical.
            "nonce": secrets.token_hex(16),
        }
        try:
            return pyjwt.encode(
                payload, self._private_key, algorithm=self.algorithm, headers=headers
            )
        except Exception as exc:  # pragma: no cover - depends on key material
            raise AuthenticationError(f"failed to sign request JWT: {exc}") from exc

    def headers(self, method: str, url: str) -> dict:
        return {
            "Authorization": f"Bearer {self.token(method, url)}",
            "Content-Type": "application/json",
            "User-Agent": "coinbot/1.0",
        }

    def websocket_token(self) -> str:
        """JWT for the WebSocket feed, which carries no ``uri`` claim."""
        now = int(time.time())
        payload = {
            "sub": self.api_key,
            "iss": "cdp",
            "nbf": now,
            "exp": now + self.ttl_seconds,
        }
        headers = {"kid": self.api_key, "nonce": secrets.token_hex(16)}
        try:
            return pyjwt.encode(
                payload, self._private_key, algorithm=self.algorithm, headers=headers
            )
        except Exception as exc:  # pragma: no cover
            raise AuthenticationError(f"failed to sign websocket JWT: {exc}") from exc

    def __repr__(self) -> str:  # pragma: no cover - display only
        tail = self.api_key[-6:] if len(self.api_key) > 6 else "?"
        return f"<CoinbaseAuth alg={self.algorithm} key=...{tail}>"


__all__ = ["CoinbaseAuth"]
