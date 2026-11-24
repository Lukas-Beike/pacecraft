"""Frozen signatures of the schema shapes written by supported releases.

The baseline (v26) and the released later schemas are frozen as digests: the
SHA-256 of the normalized schema signature (object type, name, table and
whitespace-normalized SQL). The baseline's comes from its frozen fixture in
tests/fixtures; all are hard-coded here, so the runtime never reads fixture files.
The current shape is deliberately not frozen: it is derived from the live
schema in backend.db.schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Final

from backend.db.schema import CURRENT_SCHEMA_VERSION

type SchemaSignature = tuple[tuple[str, str, str, str], ...]

# Schema version 26, release 2.0.5: the only supported baseline; the digest
# freezes it for its upgrade step.
SHAPE_SCHEMA_V26: Final = "schema-v26"
# Schema version 27, since release 2.1.2: the result of the baseline's
# product step. Its digest lets a v27 database take the later steps.
SHAPE_SCHEMA_V27: Final = "schema-v27"
# Schema version 28, since release 2.1.10: v27 after its calendar
# decision step. Its digest lets a v28 database take the later steps.
SHAPE_SCHEMA_V28: Final = "schema-v28"
# Schema version 29, since release 2.1.17: v28 after its competition location step.
# Its digest lets a v29 database take the later steps.
SHAPE_SCHEMA_V29: Final = "schema-v29"
# Schema version 30, from release 2.1.20: v29 after its session curve step.
# Its digest lets a v30 database take the later steps.
SHAPE_SCHEMA_V30: Final = "schema-v30"
# Schema version 31, from release 2.1.22: v30 after its watch workout step.
# Its digest lets a v31 database take the later steps.
SHAPE_SCHEMA_V31: Final = "schema-v31"
# Schema version 32, since release 2.1.28: v31 after its diary step.
# Its digest lets a v32 database take the later steps.
SHAPE_SCHEMA_V32: Final = "schema-v32"
# Schema version 33, since release 2.1.51: v32 after its product size step.
# Its digest lets a v33 database take the later steps.
SHAPE_SCHEMA_V33: Final = "schema-v33"
# The live schema of this build, derived from code rather than frozen.
SHAPE_CURRENT: Final = "current"

RELEASED_SHAPE_DIGESTS: Final[Mapping[str, str]] = {
    SHAPE_SCHEMA_V26: "edbf1d3aa11f13e3bad44e233d9605d173c1370e6fc3ac67ad3abeda9bd0ea03",
    SHAPE_SCHEMA_V27: "203082d2a81ca06ab66307e37edbcbfe83e9a9c4e7b442124e360124175f193c",
    SHAPE_SCHEMA_V28: "3c06ad06de8948015a0508c6cae46662971ce58b06e919b3f964b662d62cc0e9",
    SHAPE_SCHEMA_V29: "26e479545cd3331930a836e145facea50f2701c695eb15f6ca2e34a0dfe84628",
    SHAPE_SCHEMA_V30: "e680b95e585bb238ddbf783aef80a6be890fe63295d07eeea1e17df6b86379ad",
    SHAPE_SCHEMA_V31: "8de2c800f630dd9819070e46ae4ccde22342cdb64a24b35c151bd7efe9007c11",
    SHAPE_SCHEMA_V32: "5c96e07d4ba532222130a559c2ee1f0b53011cfc437d0f703ab00ede7ae35e69",
    SHAPE_SCHEMA_V33: "ae6a8f2f9500b7cbc41404660a093588f98d94b4abb1170721e760cdd8eadde9",
}

# Releases before 2.0.5 wrote lower versions; they are rejected unchanged.
OLDEST_SUPPORTED_SCHEMA_VERSION: Final = 26

# Stored PRAGMA user_version -> schema shapes that version is allowed to hold.
# When CURRENT_SCHEMA_VERSION is bumped after a release, freeze the released
# previous shape's digest for its version here and add one upgrade step from it; a schema that never reached
# production gets no frozen shape of its own.
ACCEPTED_SHAPES_BY_VERSION: Final[Mapping[int, frozenset[str]]] = {
    26: frozenset({SHAPE_SCHEMA_V26}),
    27: frozenset({SHAPE_SCHEMA_V27}),
    28: frozenset({SHAPE_SCHEMA_V28}),
    29: frozenset({SHAPE_SCHEMA_V29}),
    30: frozenset({SHAPE_SCHEMA_V30}),
    31: frozenset({SHAPE_SCHEMA_V31}),
    32: frozenset({SHAPE_SCHEMA_V32}),
    33: frozenset({SHAPE_SCHEMA_V33}),
    CURRENT_SCHEMA_VERSION: frozenset({SHAPE_CURRENT}),
}


def signature_digest(signature: SchemaSignature) -> str:
    """Return the canonical SHA-256 digest of a normalized schema signature."""
    payload = json.dumps(
        [list(entry) for entry in signature], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def released_shape(signature: SchemaSignature) -> str | None:
    """Return the frozen released shape that matches a signature, if any."""
    digest = signature_digest(signature)
    for shape, expected in RELEASED_SHAPE_DIGESTS.items():
        if digest == expected:
            return shape
    return None
