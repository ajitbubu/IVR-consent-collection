"""Evidence storage: call recordings and daily chain digests.

Local filesystem for now. Callers only see keys in and URIs out, so an
object store (S3 in ap-south-1, with Object Lock for the digests) can
replace this without touching them.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings


class AlreadyStored(Exception):
    """A write-once key already exists. Nothing was overwritten."""


def _root() -> Path:
    return Path(settings().evidence_dir).resolve()


def _path(key: str) -> Path:
    path = (_root() / key).resolve()
    if _root() not in path.parents:
        raise ValueError(f"key escapes the evidence directory: {key!r}")
    return path


def put(key: str, content: bytes, *, write_once: bool = False) -> str:
    """Store content under key and return its URI. With write_once, an
    existing key raises AlreadyStored and the file is left read-only -- the
    closest a plain filesystem gets to WORM storage."""
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "xb" if write_once else "wb") as f:
            f.write(content)
    except FileExistsError as exc:
        raise AlreadyStored(key) from exc
    if write_once:
        os.chmod(path, 0o444)
    return path.as_uri()


def exists(key: str) -> bool:
    return _path(key).is_file()


def read(uri: str) -> bytes:
    return Path(urlparse(uri).path).read_bytes()


def delete(uri: str) -> None:
    """Missing is fine: the goal is that the object is gone."""
    Path(urlparse(uri).path).unlink(missing_ok=True)
