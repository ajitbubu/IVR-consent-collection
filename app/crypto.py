"""Hashing, HMAC lookup keys, and attribute encryption at rest.

The encryption here is a stand-in with the right shape: a real deployment
uses a KMS data key per record rather than a single key from the environment.
The interface is what matters -- swapping the backend should not touch callers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from app.config import settings


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def phone_hash(phone_e164: str) -> bytes:
    """Lookup key that does not expose the number itself."""
    return hmac.new(settings().phone_hmac_key, phone_e164.encode(), hashlib.sha256).digest()


def canonical_json(payload: Any) -> bytes:
    """Stable bytes for hashing. Key order and separators must never drift,
    or every historical hash stops verifying."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def encrypt_attribute(plaintext: str) -> bytes:
    """XOR-with-keystream placeholder. Shape is right, strength is not --
    replace with KMS envelope encryption before this touches real data."""
    nonce = os.urandom(16)
    stream = _keystream(nonce, len(plaintext.encode()))
    body = bytes(a ^ b for a, b in zip(plaintext.encode(), stream))
    return nonce + body


def decrypt_attribute(blob: bytes) -> str:
    nonce, body = blob[:16], blob[16:]
    stream = _keystream(nonce, len(body))
    return bytes(a ^ b for a, b in zip(body, stream)).decode()


def _keystream(nonce: bytes, length: int) -> bytes:
    key = settings().attribute_key
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]
