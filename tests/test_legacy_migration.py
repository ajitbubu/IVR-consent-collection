import hashlib

import pytest

from app.crypto import decrypt_attribute, decrypt_legacy_attribute, sha256
from app.identity import get_or_create_principal, set_attribute
from app.migrate_legacy_attributes import migrate
from app.models import IdentityAttribute
from app.ucm import _live_attrs


def legacy_blob(value, nonce, key=b"original-attribute-key"):
    body = value.encode()
    stream = b"".join(hashlib.sha256(key + nonce + n.to_bytes(4, "big")).digest()
                      for n in range((len(body) + 31) // 32))
    return nonce + bytes(a ^ b for a, b in zip(body, stream))


@pytest.mark.parametrize("prefix", [0, 1, 2, 255])
@pytest.mark.parametrize("value", ["", "Asha Rao", "नाम " * 50])
def test_original_ciphertext_decoder_including_version_collisions(prefix, value):
    blob = legacy_blob(value, bytes([prefix]) + b"n" * 15)
    assert decrypt_legacy_attribute(blob, b"original-attribute-key") == value


def test_migration_restores_ucm_attributes_and_is_not_reapplied(db, marketing_purpose):
    dp = get_or_create_principal(db, "+919876543210")
    row = set_attribute(db, dp, "name", "unused", "crm")
    # A legacy nonce starting with v1 cannot be classified by its first byte.
    row.value_enc = legacy_blob("Asha Rao", b"\x01" + b"n" * 15)
    db.commit()
    manifest = {row.id: sha256(row.value_enc).hex()}
    assert migrate(db, manifest, b"original-attribute-key") == 1
    db.commit()
    assert _live_attrs(db, dp.id) == {"name": "Asha Rao"}
    assert decrypt_attribute(row.value_enc) == "Asha Rao"
    from app.consent_service import create_session, record_notice_served, record_decision
    from app.ucm import build_payload
    sess = create_session(db, direction="ivr_inbound", phone_raw=dp.phone_e164,
                          purpose_code=marketing_purpose.code)
    record_notice_served(db, sess)
    consent = record_decision(db, sess, digit="1").consent
    db.commit()
    assert build_payload(db, consent, dp, marketing_purpose)["data_principal"]["name"] == "Asha Rao"
    with pytest.raises(ValueError, match="changed"):
        migrate(db, manifest, b"original-attribute-key")


def test_migration_validates_entire_manifest_before_mutation(db):
    dp = get_or_create_principal(db, "+919876543210")
    row = set_attribute(db, dp, "name", "unused", "crm")
    original = legacy_blob("Asha", b"n" * 16)
    row.value_enc = original
    db.commit()
    with pytest.raises(ValueError):
        migrate(db, {row.id: sha256(original).hex(), "z" * 26: "0" * 64},
                b"original-attribute-key")
    db.rollback()
    assert db.get(IdentityAttribute, row.id).value_enc == original


def test_migration_dry_run_leaves_ciphertext_unchanged(db):
    dp = get_or_create_principal(db, "+919876543210")
    row = set_attribute(db, dp, "name", "unused", "crm")
    original = legacy_blob("Asha", b"n" * 16)
    row.value_enc = original
    db.commit()
    migrate(db, {row.id: sha256(original).hex()}, b"original-attribute-key")
    db.rollback()
    assert row.value_enc == original
