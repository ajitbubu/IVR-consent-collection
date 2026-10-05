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
