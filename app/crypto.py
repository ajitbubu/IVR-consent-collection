"""Hashing, HMAC lookup keys, and attribute encryption at rest.

Attributes use envelope encryption: every value gets its own AES-256-GCM data
key, and only the wrapped data key is stored next to the ciphertext. The key
that wraps it lives behind KmsPort -- LocalKms derives it from ATTRIBUTE_KEY
for development; production swaps in a real KMS without touching callers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


class KmsPort(Protocol):
    """Wraps and unwraps data keys. The key-encryption key never leaves it."""

    def wrap(self, data_key: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKms:
    """Development KMS: AES-GCM key wrap under a key derived from
    ATTRIBUTE_KEY. Same interface a cloud KMS client would sit behind."""

    def __init__(self, secret: bytes | None = None):
        self._kek = AESGCM(sha256(secret or settings().attribute_key))

    def wrap(self, data_key: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._kek.encrypt(nonce, data_key, b"dek")

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._kek.decrypt(wrapped[:_NONCE], wrapped[_NONCE:], b"dek")


_NONCE = 12
_V1 = b"\x01"
_VERSION = b"\x02"
_WRAPPED_LEN = _NONCE + 32 + 16  # historical v1 LocalKms only
_LENGTH_BYTES = 4


def encrypt_attribute(plaintext: str, kms: KmsPort | None = None) -> bytes:
    """v2: version | uint32 wrapped-key length | wrapped key | nonce | body.

    The header is authenticated as AAD; wrapped keys remain opaque to callers.
    """
    kms = kms or LocalKms()
    data_key = AESGCM.generate_key(bit_length=256)
    wrapped = kms.wrap(data_key)
    if not wrapped:
        raise ValueError("empty wrapped data key")
    header = _VERSION + len(wrapped).to_bytes(_LENGTH_BYTES, "big")
    nonce = os.urandom(_NONCE)
    body = AESGCM(data_key).encrypt(nonce, plaintext.encode(), header)
    return header + wrapped + nonce + body


def decrypt_attribute(blob: bytes, kms: KmsPort | None = None) -> str:
    """Read v1/v2 only. Never fall back to unauthenticated legacy decoding.

    Unversioned XOR rows must be explicitly migrated with their original key;
    their random nonce can start with any version byte.
    """
    if blob[:1] == _V1:
        offset, wrapped_len, aad = 1, _WRAPPED_LEN, _V1
    elif blob[:1] == _VERSION:
        if len(blob) < 1 + _LENGTH_BYTES:
            raise ValueError("truncated attribute header")
        offset = 1 + _LENGTH_BYTES
        wrapped_len = int.from_bytes(blob[1:offset], "big")
        aad = blob[:offset]
    else:
        raise ValueError("unknown attribute encryption version; migrate legacy rows explicitly")
    if wrapped_len == 0 or len(blob) < offset + wrapped_len + _NONCE + 16:
        raise ValueError("invalid attribute envelope length")
    kms = kms or LocalKms()
    end = offset + wrapped_len
    wrapped, nonce, body = blob[offset:end], blob[end:end + _NONCE], blob[end + _NONCE:]
    return AESGCM(kms.unwrap(wrapped)).decrypt(nonce, body, aad).decode()


def decrypt_legacy_attribute(blob: bytes, key: bytes) -> str:
    """Migration-only decoder for the original nonce(16) + XOR format.

    This format has no authentication or reliable discriminator. Call only for
    rows independently identified as legacy, never after an AES read failure.
    """
    if len(blob) < 16:
        raise ValueError("truncated legacy attribute")
    nonce, body = blob[:16], blob[16:]
    stream = bytearray()
    counter = 0
    while len(stream) < len(body):
        stream.extend(sha256(key + nonce + counter.to_bytes(4, "big")))
        counter += 1
    return bytes(a ^ b for a, b in zip(body, stream)).decode()
