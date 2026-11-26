"""Refill rule (proposal slide 8).

FHIR counts repeats *excluding* the original dispense:
    30 tablets + 3 repeats (numberOfRepeatsAllowed = 3) = 4 fills.

A *fill* is complete when the completed MedicationDispenses for the request add
up to the prescribed quantity (dispenseRequest.quantity). So:

- a partial fill leaves the refill count unchanged until the fill is complete;
- a substitution counts toward the current fill by its quantity (entered in the
  prescribed product's units) and never uses up a repeat of its own;
- once all 1 + repeats fills are complete, any further dispense is blocked.

The count is always recomputed from the dispense history, never stored, so the
blocking rule, the timeline and reconciliation all read the same numbers.

If the request carries no quantity (EMR-V2 before the FHIR additions), every
completed dispense is treated as one whole fill and quantity_known is False.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fill:
    number: int                  # 1 = original dispense, 2.. = repeats
    dispense_ids: list[str] = field(default_factory=list)
    quantity: float = 0.0
    complete: bool = False
    completed_at: str | None = None
    started_at: str | None = None
    refills_left_after: int | None = None


@dataclass
class RefillStatus:
    prescribed_quantity: float | None
    unit: str
    repeats_allowed: int
    fills_allowed: int
    fills: list[Fill]
    fills_completed: int
    current_fill_quantity: float
    refills_left: int
    blocked: bool
    max_next_quantity: float | None   # None = quantity unknown
    quantity_known: bool

    @property
    def message(self) -> str:
        if self.blocked:
            return (f"Blocked: all {self.fills_allowed} fills "
                    f"(original + {self.repeats_allowed} repeats) have been dispensed.")
        if self.current_fill_quantity > 0:
            return (f"Fill {self.fills_completed + 1} in progress: "
                    f"{_fmt(self.current_fill_quantity)} of {_fmt(self.prescribed_quantity)} {self.unit} dispensed. "
                    f"Refills left: {self.refills_left}.")
        return f"Refills left: {self.refills_left} (fill {self.fills_completed + 1} of {self.fills_allowed} next)."


def _fmt(x: float | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def prescribed(request: dict) -> tuple[float | None, str, int]:
    dr = request.get("dispenseRequest") or {}
    q = dr.get("quantity") or {}
    qty = float(q["value"]) if q.get("value") is not None else None
    unit = q.get("unit") or q.get("code") or "units"
    repeats = int(dr.get("numberOfRepeatsAllowed") or 0)
    return qty, unit, repeats


def completed_dispenses(dispenses: list[dict]) -> list[dict]:
    done = [d for d in dispenses if d.get("status") == "completed"]
    return sorted(done, key=lambda d: (d.get("whenHandedOver") or "", d.get("id") or ""))


def refills_left_for(fills_completed: int, repeats: int) -> int:
    # The original fill does not use a repeat: after fill 1 all repeats remain.
    return max(0, repeats - max(0, fills_completed - 1))


def refill_status(request: dict, dispenses: list[dict]) -> RefillStatus:
    qty, unit, repeats = prescribed(request)
    fills_allowed = repeats + 1
    fills: list[Fill] = []
    current: Fill | None = None
    completed = 0

    for d in completed_dispenses(dispenses):
        amount = float((d.get("quantity") or {}).get("value") or 0)
        if current is None:
            current = Fill(number=completed + 1, started_at=d.get("whenHandedOver"))
            fills.append(current)
        current.dispense_ids.append(d.get("id"))
        current.quantity += amount
        if qty is None or current.quantity >= qty - 1e-9:
            current.complete = True
            current.completed_at = d.get("whenHandedOver")
            completed += 1
            current.refills_left_after = refills_left_for(completed, repeats)
            current = None

    in_progress = current.quantity if current else 0.0
    blocked = completed >= fills_allowed
    if blocked or qty is None:
        max_next = 0.0 if blocked else None
    else:
        max_next = qty - in_progress

    return RefillStatus(
        prescribed_quantity=qty,
        unit=unit,
        repeats_allowed=repeats,
        fills_allowed=fills_allowed,
        fills=fills,
        fills_completed=completed,
        current_fill_quantity=in_progress,
        refills_left=refills_left_for(completed, repeats),
        blocked=blocked,
        max_next_quantity=max_next,
        quantity_known=qty is not None,
    )


def supply_type(status: RefillStatus, quantity: float) -> tuple[str, str]:
    """v3 ActPharmacySupplyType for the next dispense of `quantity`."""
    first = status.fills_completed == 0
    completes = (status.max_next_quantity is None) or quantity >= (status.max_next_quantity - 1e-9)
    starts_fresh = status.current_fill_quantity == 0
    if first:
        if completes and starts_fresh:
            return "FF", "First Fill"
        return ("FFC", "First Fill - Complete") if completes else ("FFP", "First Fill - Part Fill")
    if completes and starts_fresh:
        return "RF", "Refill"
    return ("RFC", "Refill - Complete") if completes else ("RFP", "Refill - Part Fill")
