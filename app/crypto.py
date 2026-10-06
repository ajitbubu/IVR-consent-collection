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
_VERSION = b"\x01"
_WRAPPED_LEN = _NONCE + 32 + 16  # nonce + data key + GCM tag (LocalKms)


def encrypt_attribute(plaintext: str, kms: KmsPort | None = None) -> bytes:
    """version | wrapped data key | nonce | ciphertext+tag."""
    kms = kms or LocalKms()
    data_key = AESGCM.generate_key(bit_length=256)
    wrapped = kms.wrap(data_key)
    nonce = os.urandom(_NONCE)
    body = AESGCM(data_key).encrypt(nonce, plaintext.encode(), _VERSION)
    return _VERSION + wrapped + nonce + body


def decrypt_attribute(blob: bytes, kms: KmsPort | None = None) -> str:
    """Raises cryptography.exceptions.InvalidTag if anything was altered."""
    if blob[:1] != _VERSION:
        raise ValueError("unknown attribute encryption version")
    kms = kms or LocalKms()
    wrapped = blob[1:1 + _WRAPPED_LEN]
    nonce = blob[1 + _WRAPPED_LEN:1 + _WRAPPED_LEN + _NONCE]
    body = blob[1 + _WRAPPED_LEN + _NONCE:]
    return AESGCM(kms.unwrap(wrapped)).decrypt(nonce, body, _VERSION).decode()
