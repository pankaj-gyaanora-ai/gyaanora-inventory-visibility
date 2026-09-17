"""Source adapters -- the plugin layer.

Each adapter answers exactly one question: *what signed bucket quantities does
my feed contribute?*  It knows its own file format and its own business
statuses, and it knows nothing about availability, flags, the API or any other
feed. That isolation is why source #5 is additive work, not a refactor.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import parse_ts
from .models import CanonicalRecord, QuarantinedRow, SourceLoadError


class SourceAdapter:
    """Interface every feed implements."""

    def __init__(self, spec: dict, root: Path):
        self.spec = spec
        self.name = spec["name"]
        self.label = spec.get("label", spec["name"])
        self.criticality = spec.get("criticality", "optional")
        self.path = root / spec["path"]
        self.as_of = parse_ts(spec["export_as_of"])
        self.stats: dict[str, int] = {}

    def load(self, quarantine: list[QuarantinedRow]) -> Iterable[CanonicalRecord]:
        raise NotImplementedError

    def _open(self):
        if not self.path.exists():
            raise SourceLoadError(f"{self.name}: feed not found at {self.path}")
        return self.path

    @staticmethod
    def _int(value, row_ref, source, field, quarantine, sku=None):
        """Parse a quantity or quarantine the row. Never crash the run."""
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            quarantine.append(QuarantinedRow(
                source=source, row_ref=row_ref, reason="QTY_NOT_NUMERIC",
                field=field, raw_value=value, sku=sku))
            return None


class WmsCsvAdapter(SourceAdapter):
    """Physical stock on the shelf, plus the damaged/blocked portion of it.

    Note damaged_qty is a SUBSET of on_hand in the WMS export, so it is emitted
    as a separate negative-signed UNSELLABLE bucket rather than netted here.
    """

    def load(self, quarantine):
        rows_in = accepted = 0
        with open(self._open(), newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                rows_in += 1
                ref = f"wms:row{i}"
                sku = (row.get("sku") or "").strip()
                if not sku:
                    quarantine.append(QuarantinedRow("wms", ref, "MISSING_SKU", "sku", row.get("sku")))
                    continue
                qty = self._int(row.get("on_hand"), ref, "wms", "on_hand", quarantine, sku)
                if qty is None:
                    continue
                loc = (row.get("warehouse") or "").strip()
                uom = (row.get("uom") or "EA").strip()
                counted = row.get("last_counted") or self.spec["export_as_of"]
                accepted += 1
                yield CanonicalRecord(
                    sku=sku, location=loc, bucket="ON_HAND", quantity=qty,
                    source="wms", source_record_id=f"wms:{sku}:{loc}",
                    as_of=self.as_of, uom=uom,
                    meta={"last_counted": str(counted), "row": i})
                dmg = self._int(row.get("damaged_qty") or 0, ref, "wms", "damaged_qty", quarantine, sku)
                if dmg:
                    yield CanonicalRecord(
                        sku=sku, location=loc, bucket="UNSELLABLE", quantity=dmg,
                        source="wms", source_record_id=f"wms:{sku}:{loc}:dmg",
                        as_of=self.as_of, uom=uom, meta={"reason": "damaged", "row": i})
        self.stats = {"rows_in": rows_in, "accepted": accepted}


class OrderJsonAdapter(SourceAdapter):
    """Customer demand already committed against on-hand stock.

    DOUBLE-COUNT TRAP: a SHIPPED line has physically left the building, so WMS
    on-hand already excludes it. Subtracting it again understates availability.
    Only `reserving_statuses` create RESERVED records.
    """

    def load(self, quarantine):
        payload = json.loads(Path(self._open()).read_text())
        reserving = {s.upper() for s in self.spec.get("reserving_statuses", [])}
        orders = payload.get("orders", [])
        lines_in = reserving_lines = skipped = 0
        for order in orders:
            status = (order.get("status") or "").upper()
            oid = order.get("order_id", "?")
            for n, line in enumerate(order.get("lines", []), start=1):
                lines_in += 1
                ref = f"{oid}/{n}"
                if status not in reserving:
                    skipped += 1
                    continue
                sku = (line.get("sku") or "").strip()
                qty = self._int(line.get("qty"), ref, "orders", "qty", quarantine, sku)
                if qty is None or not sku:
                    continue
                reserving_lines += 1
                yield CanonicalRecord(
                    sku=sku, location=(line.get("warehouse") or "").strip(),
                    bucket="RESERVED", quantity=qty, source="orders",
                    source_record_id=ref, as_of=self.as_of,
                    uom=(line.get("uom") or "EA"),
                    meta={"order_id": oid, "status": status,
                          "customer": order.get("customer"),
                          "promised_date": order.get("promised_date"),
                          "otif_penalty": order.get("otif_penalty", False)})
        self.stats = {"orders_in": len(orders), "lines_in": lines_in,
                      "reserving_lines": reserving_lines,
                      "non_reserving_skipped": skipped}


class ShipmentCsvAdapter(SourceAdapter):
    """Units on an open supplier PO. NOT sellable today -- ATP only.

    DOUBLE-COUNT TRAP: a RECEIVED PO is already inside the WMS on-hand number.
    Only `open_statuses` create INBOUND records.
    """

    def load(self, quarantine):
        open_statuses = {s.upper() for s in self.spec.get("open_statuses", [])}
        rows_in = open_rows = received_skipped = 0
        with open(self._open(), newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                rows_in += 1
                ref = f"{row.get('po_id','?')}"
                status = (row.get("status") or "").upper()
                if status not in open_statuses:
                    received_skipped += 1
                    continue
                sku = (row.get("sku") or "").strip()
                ordered = self._int(row.get("qty_ordered"), ref, "shipments", "qty_ordered", quarantine, sku)
                received = self._int(row.get("qty_received") or 0, ref, "shipments", "qty_received", quarantine, sku)
                if ordered is None or received is None:
                    continue
                outstanding = max(ordered - received, 0)   # PARTIAL: only the balance
                if outstanding == 0:
                    continue
                open_rows += 1
                yield CanonicalRecord(
                    sku=sku, location=(row.get("warehouse") or "").strip(),
                    bucket="INBOUND", quantity=outstanding, source="shipments",
                    source_record_id=ref, as_of=self.as_of,
                    uom=(row.get("uom") or "EA"),
                    meta={"po_id": row.get("po_id"), "eta": row.get("eta"),
                          "status": status, "supplier": row.get("supplier"),
                          "qty_ordered": ordered, "qty_received": received})
        self.stats = {"rows_in": rows_in, "open_pos": open_rows,
                      "received_skipped": received_skipped}


class ReturnsJsonAdapter(SourceAdapter):
    """FEED #4 -- added in month two.

    The whole integration: this class plus a config block. Statuses map to
    buckets in YAML, so if the business later decides pending-QC returns should
    count toward 14-day ATP, that is `atp_sign: 1` in a file, not a release.

    Same double-count trap as received POs: QC_PASSED_RESTOCKED units are
    already inside the WMS count, so they map to IGNORE.
    """

    def load(self, quarantine):
        payload = json.loads(Path(self._open()).read_text())
        mapping = {k.upper(): v for k, v in (self.spec.get("status_bucket_map") or {}).items()}
        rows = payload.get("returns", [])
        emitted = ignored = 0
        for r in rows:
            status = (r.get("status") or "").upper()
            bucket = mapping.get(status)
            ref = r.get("rma_id", "?")
            if bucket is None:
                quarantine.append(QuarantinedRow(
                    "returns", ref, "UNMAPPED_STATUS", "status", status,
                    action="row_dropped", sku=r.get("sku")))
                continue
            if bucket == "IGNORE":
                ignored += 1
                continue
            sku = (r.get("sku") or "").strip()
            qty = self._int(r.get("qty"), ref, "returns", "qty", quarantine, sku)
            if qty is None:
                continue
            emitted += 1
            yield CanonicalRecord(
                sku=sku, location=(r.get("warehouse") or "").strip(),
                bucket=bucket, quantity=qty, source="returns",
                source_record_id=ref, as_of=self.as_of,
                meta={"rma_id": ref, "status": status, "reason": r.get("reason")})
        self.stats = {"rows_in": len(rows), "emitted": emitted,
                      "ignored_already_counted": ignored}


def build_adapter(spec: dict, root: Path) -> SourceAdapter:
    """Resolve `inv_visibility.adapters.WmsCsvAdapter` from config to a live object."""
    module_path, _, cls_name = spec["adapter"].rpartition(".")
    import importlib
    cls = getattr(importlib.import_module(module_path), cls_name)
    return cls(spec, root)
