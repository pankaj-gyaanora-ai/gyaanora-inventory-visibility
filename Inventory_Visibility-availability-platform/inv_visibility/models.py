"""Canonical model.

Every source feed is translated into CanonicalRecord. Nothing downstream of the
adapter layer ever sees a source-specific shape again -- that boundary is what
makes a fifth or sixth feed a config change instead of a rewrite.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CanonicalRecord:
    """A signed quantity contributed by one source into one bucket."""

    sku: str
    location: str
    bucket: str                 # ON_HAND / RESERVED / INBOUND / ...
    quantity: int
    source: str                 # which feed produced it
    source_record_id: str       # lineage: the exact row/line it came from
    as_of: datetime
    uom: str = "EA"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


@dataclass
class QuarantinedRow:
    """A bad input row set aside with a reason. Never silently dropped, never
    allowed to crash a 5,000-SKU run."""

    source: str
    row_ref: str
    reason: str
    field: str | None = None
    raw_value: Any = None
    action: str = "row_dropped"
    sku: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Exception_:
    sku: str
    flag: str
    severity: str               # critical | warning | info
    message: str
    evidence: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SourceLoadError(RuntimeError):
    """Raised when a source feed cannot be read at all."""
