"""Canonical repository-wide payload hashing; extracted from prior per-module duplicates.

SHA-256 over Pydantic's own JSON encoding (field-declaration order, exact-digit Decimal
strings, ISO-8601 datetimes, string UUIDs/enums). Any future change to this primitive is a
cross-cutting migration affecting every persisted *_sha256 field in Parts 2-5A, not a
Part 5B-local decision.
"""

import hashlib

from pydantic import BaseModel


def digest(value: BaseModel) -> str:
    return hashlib.sha256(value.model_dump_json().encode()).hexdigest()
