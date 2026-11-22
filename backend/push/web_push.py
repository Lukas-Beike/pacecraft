"""Standard Web Push transport: VAPID (RFC 8292) and aes128gcm payloads (RFC 8291).

The server only sends outbound requests to the browser's push service; the
payload is encrypted for the subscribing browser, so the push service sees no
text. Only the Android browsers the athlete uses are accepted: Chrome (FCM) and
Firefox (Mozilla autopush, #3914). Keys and endpoints never reach a log.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from backend.providers import http as provider_http

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ec

# ``cryptography`` is imported where it is used, so importing the app (and the
# tests that never send) needs no native crypto library.

# The push services of Chrome and Firefox on Android; every other endpoint is refused.
PUSH_HOSTS: Final = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
# A contact claim without personal data; FCM and autopush accept any mailto.
VAPID_SUBJECT: Final = "mailto:push@pacecraft.invalid"
_RECORD_SIZE: Final = 4096
_TIMEOUT_SECONDS: Final = 10


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_private_key() -> str:
    """A new VAPID private key as its base64url 32-byte scalar."""
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return b64url(key.private_numbers().private_value.to_bytes(32, "big"))


def _private_key(private: str) -> ec.EllipticCurvePrivateKey:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.derive_private_key(
        int.from_bytes(unb64url(private), "big"), ec.SECP256R1()
    )


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def public_key(private: str) -> str:
    """The base64url uncompressed public point the browser subscribes with."""
    return b64url(_point(_private_key(private).public_key()))


def supported_endpoint(endpoint: Any) -> bool:
    """An https endpoint of one of the accepted push services."""
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    parts = urlsplit(endpoint)
    return parts.scheme == "https" and parts.hostname in PUSH_HOSTS and not parts.port


def encrypt(
    payload: bytes,
    p256dh: str,
    auth: str,
    *,
    sender_key: str | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The aes128gcm body for one browser subscription (RFC 8291, single record).

    ``sender_key`` and ``salt`` are fresh per message; tests pass the RFC's.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    receiver = unb64url(p256dh)
    secret = unb64url(auth)
    sender = _private_key(sender_key or generate_private_key())
    sender_public = _point(sender.public_key())
    shared = sender.exchange(
        ec.ECDH(),
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver),
    )
    ikm = HKDF(
        hashes.SHA256(), 32, secret, b"WebPush: info\x00" + receiver + sender_public
    ).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(
        ikm
    )
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    header = salt + struct.pack("!IB", _RECORD_SIZE, len(sender_public)) + sender_public
    return header + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)


def vapid_authorization(endpoint: str, private: str, now: float) -> str:
    """The ``Authorization`` header value: an ES256 JWT for the endpoint's origin."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(now) + 12 * 3600,
        "sub": VAPID_SUBJECT,
    }
    segments = [
        b64url(json.dumps(value, separators=(",", ":")).encode())
        for value in ({"typ": "JWT", "alg": "ES256"}, claims)
    ]
    signing_input = ".".join(segments).encode("ascii")
    r, s = decode_dss_signature(
        _private_key(private).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    )
    token = ".".join([*segments, b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))])
    return f"vapid t={token}, k={public_key(private)}"


def send(
    subscription: dict[str, Any],
    message: dict[str, Any],
    private: str,
    *,
    ttl: int,
    opener: Callable[..., Any] = provider_http.urlopen,
    now: Callable[[], float] = time.time,
) -> int:
    """Deliver one encrypted message; the push service's HTTP status (0: no answer)."""
    endpoint = subscription["endpoint"]
    if not supported_endpoint(endpoint):
        return 0
    body = encrypt(
        json.dumps(message, ensure_ascii=False).encode("utf-8"),
        subscription["p256dh"],
        subscription["auth"],
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": vapid_authorization(endpoint, private, now()),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Urgency": "normal",
        },
    )
    try:
        with opener(request, timeout=_TIMEOUT_SECONDS) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except OSError, ValueError:
        return 0
