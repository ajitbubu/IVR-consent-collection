"""Explicit, atomic re-encryption of independently identified legacy rows.

Manifest: JSON object mapping identity_attribute IDs to SHA-256 of their
original ciphertext. Build it from a pre-envelope backup, never by guessing
from the first byte. No plaintext or key is written to output.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.crypto import decrypt_attribute, decrypt_legacy_attribute, encrypt_attribute, sha256
from app.db import session_scope
from app.models import IdentityAttribute


def migrate(db: Session, manifest: dict[str, str], legacy_key: bytes) -> int:
    # Validate every row before changing any; keep locks until the caller commits.
    replacements = []
    for row_id, digest in sorted(manifest.items()):
        row = db.get(IdentityAttribute, row_id, with_for_update=True)
        if row is None or sha256(row.value_enc).hex() != digest:
            raise ValueError(f"missing or changed legacy row: {row_id}")
        value = decrypt_legacy_attribute(row.value_enc, legacy_key)
        encrypted = encrypt_attribute(value)
        if decrypt_attribute(encrypted) != value:
            raise ValueError("re-encryption verification failed")
        replacements.append((row, encrypted))
    for row, encrypted in replacements:
        row.value_enc = encrypted
    db.flush()
    return len(replacements)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true", help="commit; default is rollback-only dry run")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if not isinstance(manifest, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and len(v) == 64
        for k, v in manifest.items()
    ):
        parser.error("manifest must map row IDs to ciphertext SHA-256 hex digests")
    key = os.environ["LEGACY_ATTRIBUTE_KEY"].encode()
    with session_scope() as db:
        count = migrate(db, manifest, key)
        if not args.apply:
            db.rollback()
    print(f"{'Migrated' if args.apply else 'Validated (rolled back)'} {count} legacy rows")


if __name__ == "__main__":
    main()
