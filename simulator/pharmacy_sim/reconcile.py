"""Medication reconciliation (proposal slide 9): match, flag, resolve; evidence kept.

Compares, per patient, three separate facts:
    ordered   MedicationRequest   (status active)
    supplied  MedicationDispense  (status completed)
    reported  MedicationStatement (status active = taking, not-taken = not taking)

Flags:
    dose_mismatch            ordered dose differs from what the patient reports
    not_dispensed_not_taken  active order older than the grace period, never
                             dispensed, patient not taking it
    unrecorded_medication    patient takes something with no order (e.g. OTC)
    duplicate_therapy        two different drugs from the same therapeutic group
                             (catalogue.duplicate_groups), or the same drug
                             ordered twice

Each flag has a stable id built from its type and evidence, so a resolution
recorded once stays attached when the check runs again.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from .catalogue import Catalogue, dose_mg, med_text, norm
from .fhir import ref_id
from .timeline import to_date

LABELS = {
    "dose_mismatch": "Dose mismatch",
    "not_dispensed_not_taken": "Not dispensed, not taken",
    "unrecorded_medication": "Unrecorded medication",
    "duplicate_therapy": "Duplicate therapy",
}


@dataclass
class Flag:
    type: str
    message: str
    evidence: list[str]                 # FHIR references, e.g. MedicationRequest/rx-1
    id: str = ""
    resolution: dict | None = None

    def __post_init__(self):
        if not self.id:
            raw = self.type + "|" + "|".join(sorted(self.evidence))
            self.id = hashlib.sha1(raw.encode()).hexdigest()[:10]

    @property
    def label(self) -> str:
        return LABELS[self.type]


@dataclass
class _Item:
    ref: str
    text: str
    key: str
    product: object
    resource: dict = field(repr=False)


def _item(cat: Catalogue, rtype: str, r: dict) -> _Item:
    product = cat.product_for(r.get("medicationCodeableConcept"))
    key = product.key if product else "text:" + norm(med_text(r))
    return _Item(f"{rtype}/{r['id']}", med_text(r), key, product, r)


def _fmt_mg(v: float) -> str:
    return f"{int(v)} mg" if float(v).is_integer() else f"{v:g} mg"


def reconcile(cat: Catalogue, requests: list[dict], dispenses: list[dict],
              statements: list[dict], today: date | None = None, grace_days: int = 3) -> list[Flag]:
    """An order younger than `grace_days` is not yet flagged as not dispensed."""
    today = today or date.today()
    orders = [_item(cat, "MedicationRequest", r) for r in requests if r.get("status") == "active"]
    taking = [_item(cat, "MedicationStatement", s) for s in statements if s.get("status") == "active"]
    not_taking = [_item(cat, "MedicationStatement", s) for s in statements if s.get("status") == "not-taken"]
    dispensed_rx = {
        ref_id(p.get("reference"))
        for d in dispenses if d.get("status") == "completed"
        for p in d.get("authorizingPrescription", [])
    }
    flags: list[Flag] = []

    for o in orders:
        reports = [s for s in taking if s.key == o.key]
        # dose mismatch
        o_mg = dose_mg(o.resource, o.product)
        for s in reports:
            s_mg = dose_mg(s.resource, s.product)
            if o_mg is not None and s_mg is not None and abs(o_mg - s_mg) > 1e-6:
                flags.append(Flag("dose_mismatch",
                                  f"{o.text}: ordered {_fmt_mg(o_mg)} per dose, patient reports {_fmt_mg(s_mg)}.",
                                  [o.ref, s.ref]))
        # not dispensed, not taken
        written = to_date(o.resource.get("authoredOn"))
        old_enough = written is None or (today - written).days > grace_days
        if o.resource["id"] not in dispensed_rx and not reports and old_enough:
            said_no = [s.ref for s in not_taking if s.key == o.key]
            flags.append(Flag("not_dispensed_not_taken",
                              f"{o.text}: active order with no dispense"
                              + (" and the patient says they do not take it." if said_no
                                 else " and no report that the patient takes it."),
                              [o.ref] + said_no))

    # unrecorded medication
    ordered_keys = {o.key for o in orders}
    for s in taking:
        if s.key not in ordered_keys:
            flags.append(Flag("unrecorded_medication",
                              f"{s.text}: patient reports taking it, but there is no active order "
                              f"(for example over-the-counter use).", [s.ref]))

    # duplicate therapy across everything the patient is ordered or taking
    pool: dict[str, _Item] = {}
    for it in orders + taking:
        pool.setdefault(it.key, it)
    for a, b in combinations(pool.values(), 2):
        ga, gb = cat.duplicate_group(a.product), cat.duplicate_group(b.product)
        if ga and ga is gb:
            flags.append(Flag("duplicate_therapy",
                              f"{a.text} and {b.text} are both {ga['label']}: possible duplicate therapy.",
                              [a.ref, b.ref]))
    seen: dict[str, _Item] = {}
    for o in orders:
        if o.key in seen:
            flags.append(Flag("duplicate_therapy",
                              f"{o.text} is ordered twice ({seen[o.key].ref}, {o.ref}).",
                              [seen[o.key].ref, o.ref]))
        seen.setdefault(o.key, o)
    return flags
