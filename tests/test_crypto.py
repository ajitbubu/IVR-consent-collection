import pytest
from cryptography.exceptions import InvalidTag

from app.crypto import LocalKms, decrypt_attribute, encrypt_attribute


def test_round_trip():
    assert decrypt_attribute(encrypt_attribute("Asha Rao")) == "Asha Rao"


def test_same_value_never_encrypts_the_same_way():
    assert encrypt_attribute("asha@example.com") != encrypt_attribute("asha@example.com")


def test_ciphertext_does_not_contain_the_value():
    assert b"Asha" not in encrypt_attribute("Asha Rao")


def test_tampering_is_detected():
    blob = bytearray(encrypt_attribute("Asha Rao"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt_attribute(bytes(blob))


def test_wrong_key_cannot_decrypt():
    blob = encrypt_attribute("Asha Rao", kms=LocalKms(b"key-one"))
    with pytest.raises(InvalidTag):
        decrypt_attribute(blob, kms=LocalKms(b"key-two"))


def test_historical_v1_remains_readable():
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # Reproduce the writer from the merged PR, independent of the new writer.
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    blob = b"\x01" + LocalKms().wrap(key) + nonce + AESGCM(key).encrypt(
        nonce, "old authenticated value".encode(), b"\x01")
    assert decrypt_attribute(blob) == "old authenticated value"
    with pytest.raises(InvalidTag):
        decrypt_attribute(blob[:-1] + bytes([blob[-1] ^ 1]))


@pytest.mark.parametrize("size", [1, 32, 60, 61, 256, 4096])
def test_variable_kms_envelopes(size):
    class VariableKms:
        def wrap(self, key):
            self.key = key
            return b"k" * size

        def unwrap(self, wrapped):
            assert wrapped == b"k" * size
            return self.key

    kms = VariableKms()
    blob = encrypt_attribute("नमस्ते", kms)
    assert blob[:1] == b"\x02"
    assert int.from_bytes(blob[1:5], "big") == size
    assert decrypt_attribute(blob, kms) == "नमस्ते"


@pytest.mark.parametrize("blob", [b"", b"\x03", b"\x01", b"\x02", b"\x02\0\0\0\0",
                                  b"\x02\xff\xff\xff\xff"])
def test_invalid_envelopes_are_rejected(blob):
    with pytest.raises(ValueError):
        decrypt_attribute(blob)


def test_header_tampering_never_uses_legacy_decoder(monkeypatch):
    import app.crypto as crypto

    def forbidden(*args):
        pytest.fail("legacy fallback must never be used")

    monkeypatch.setattr(crypto, "decrypt_legacy_attribute", forbidden)
    blob = bytearray(encrypt_attribute("secret"))
    blob[4] += 1
    with pytest.raises((InvalidTag, ValueError)):
        decrypt_attribute(bytes(blob))
