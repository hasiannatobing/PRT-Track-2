"""The pharmacy workflow (proposal slide 7).

1 retrieve active prescriptions  -> PharmacyService.worklist()
2 pharmacist reviews each order  -> PharmacyService.review()
3 full, partial, hold, substitute -> PharmacyService.dispense()
4 create a linked MedicationDispense (authorizingPrescription)
5 keep history and an audit trail (state/audit.jsonl)

Rules run here, before anything is written: request must be active, refill
limit, no over-dispensing, allergy warning needs an override reason, allergy
review needs a note, substitution needs a reason.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import allergy as allergy_mod
from . import reconcile as reconcile_mod
from . import timeline as timeline_mod
from .catalogue import Catalogue, med_text
from .fhir import FhirError, ref_id
from .refills import RefillStatus, refill_status, supply_type

ACTIONS = ("full", "partial", "hold", "substitute")


class DispenseRefused(Exception):
    """A safety or workflow rule stopped the dispense. The message is shown to the pharmacist."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Review:
    request: dict
    patient: dict | None
    dispenses: list[dict]
    status: RefillStatus
    allergy: allergy_mod.AllergyCheck
    timeline: timeline_mod.Timeline


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, actor: str, action: str, **details) -> dict:
        entry = {"at": now_iso(), "actor": actor, "action": action, **details}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class FlagBook:
    """First-seen time and resolution per reconciliation flag (for time-to-resolve)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))

    def seen(self, flag: reconcile_mod.Flag, patient_id: str) -> None:
        with self._lock:
            if flag.id not in self.data:
                self.data[flag.id] = {"type": flag.type, "patient": patient_id, "evidence": flag.evidence,
                                      "first_seen": now_iso(), "resolution": None}
                self._save()
        flag.resolution = self.data[flag.id]["resolution"]

    def resolve(self, flag_id: str, actor: str, outcome: str, note: str) -> dict:
        with self._lock:
            if flag_id not in self.data:
                raise KeyError(flag_id)
            self.data[flag_id]["resolution"] = {"at": now_iso(), "by": actor, "outcome": outcome, "note": note}
            self._save()
            return self.data[flag_id]


class PharmacyService:
    def __init__(self, store, catalogue: Catalogue, state_dir: Path, today: date | None = None):
        self.store = store
        self.cat = catalogue
        self.audit = AuditLog(state_dir / "audit.jsonl")
        self.flags = FlagBook(state_dir / "flags.json")
        self._today = today

    @property
    def today(self) -> date:
        return self._today or date.today()

    # 1. worklist ---------------------------------------------------------------
    def worklist(self) -> list[Review]:
        return [self.review(r["id"]) for r in self.store.search("MedicationRequest", status="active")]

    # 2. review -----------------------------------------------------------------
    def review(self, request_id: str, product_code: str | None = None) -> Review:
        rx = self.store.read("MedicationRequest", request_id)
        pid = ref_id((rx.get("subject") or {}).get("reference"))
        try:
            patient = self.store.read("Patient", pid) if pid else None
        except FhirError:
            patient = None
        dispenses = self.store.search("MedicationDispense", prescription=f"MedicationRequest/{request_id}")
        status = refill_status(rx, dispenses)
        if product_code:
            product = self.cat.by_code(product_code)
            text = product.display if product else product_code
        else:
            product = self.cat.product_for(rx.get("medicationCodeableConcept"))
            text = med_text(rx)
        allergies = self.store.search("AllergyIntolerance", patient=f"Patient/{pid}") if pid else []
        check = allergy_mod.check(self.cat, product, text, allergies)
        tl = timeline_mod.build(rx, dispenses, status, self.today)
        return Review(rx, patient, dispenses, status, check, tl)

    # 3 + 4 + 5. dispense -------------------------------------------------------
    def dispense(self, request_id: str, action: str, pharmacist: str, quantity: float | None = None,
                 substitute_code: str | None = None, reason: str = "", allergy_note: str = "") -> dict:
        if action not in ACTIONS:
            raise DispenseRefused(f"Unknown action '{action}'.")
        pharmacist = (pharmacist or "").strip() or "unknown pharmacist"
        reason = (reason or "").strip()
        allergy_note = (allergy_note or "").strip()

        rv = self.review(request_id, substitute_code if action == "substitute" else None)
        rx, st = rv.request, rv.status

        def refuse(msg: str):
            self.audit.write(pharmacist, "dispense.refused", request=f"MedicationRequest/{request_id}",
                             requested_action=action, reason=msg)
            raise DispenseRefused(msg)

        if rx.get("status") != "active":
            refuse(f"The order is '{rx.get('status')}', not active.")

        base = {
            "resourceType": "MedicationDispense",
            "subject": rx.get("subject"),
            "authorizingPrescription": [{"reference": f"MedicationRequest/{request_id}"}],
            "medicationCodeableConcept": rx.get("medicationCodeableConcept"),
            "performer": [{"actor": {"display": pharmacist}}],
        }

        if action == "hold":
            if not reason:
                refuse("A hold needs a reason.")
            md = {**base, "status": "on-hold", "whenPrepared": now_iso(),
                  "statusReasonCodeableConcept": {"text": reason}}
            md = self.store.create(md)
            self.audit.write(pharmacist, "dispense.hold", request=f"MedicationRequest/{request_id}",
                             dispense=f"MedicationDispense/{md['id']}", reason=reason)
            return md

        if st.blocked:
            refuse(st.message)

        if action == "full":
            if st.max_next_quantity is None:
                if quantity is None or quantity <= 0:
                    refuse("The order has no prescribed quantity; enter the quantity dispensed.")
            else:
                quantity = st.max_next_quantity
        else:
            if quantity is None or quantity <= 0:
                refuse("Enter a quantity greater than 0.")
            if st.max_next_quantity is not None and quantity > st.max_next_quantity + 1e-9:
                refuse(f"Only {st.max_next_quantity:g} {st.unit} remain in the current fill.")
            if action == "partial" and st.max_next_quantity is not None and quantity >= st.max_next_quantity - 1e-9:
                refuse("That quantity completes the fill; choose 'full' instead of 'partial'.")

        if action == "substitute":
            product = self.cat.by_code(substitute_code or "")
            if not product:
                refuse("Choose a substitute product from the catalogue.")
            if not reason:
                refuse("A substitution needs a reason.")
            base["medicationCodeableConcept"] = self.cat.concept(product)
            base["substitution"] = {
                "wasSubstituted": True,
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-substanceAdminSubstitution",
                                     "code": "E", "display": "equivalent"}],
                         "text": "Substituted product, quantity given in prescribed units"},
                "reason": [{"text": reason}],
            }
        else:
            base["substitution"] = {"wasSubstituted": False}

        chk = rv.allergy
        if chk.result == "warn" and not allergy_note:
            refuse("Allergy warning: " + chk.summary() + " Override with a reason to continue.")
        if chk.result == "review" and not allergy_note:
            refuse("Allergy needs pharmacist review: " + chk.summary() + " Record your review note to continue.")

        code, display = supply_type(st, quantity)
        md = {
            **base,
            "status": "completed",
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                                 "code": code, "display": display}]},
            "quantity": {"value": quantity, "unit": st.unit},
            "whenHandedOver": now_iso(),
        }
        if expected := (rx.get("dispenseRequest") or {}).get("expectedSupplyDuration"):
            md["daysSupply"] = expected
        if chk.needs_action:
            md["contained"] = [{
                "resourceType": "DetectedIssue",
                "id": "allergy",
                "status": "final",
                "code": {"text": "Drug-allergy " + ("warning" if chk.result == "warn" else "review")},
                "severity": "high" if chk.result == "warn" else "moderate",
                "detail": chk.summary(),
                "mitigation": [{
                    "action": {"text": ("Overridden: " if chk.result == "warn" else "Reviewed: ") + allergy_note},
                    "date": now_iso(),
                    "author": {"display": pharmacist},
                }],
            }]
            md["detectedIssue"] = [{"reference": "#allergy"}]
        if reason and action != "substitute":
            md["note"] = [{"text": reason}]

        md = self.store.create(md)
        after = self.review(request_id).status
        self.audit.write(pharmacist, f"dispense.{action}", request=f"MedicationRequest/{request_id}",
                         dispense=f"MedicationDispense/{md['id']}", quantity=quantity, supply_type=code,
                         allergy=chk.result, allergy_note=allergy_note or None, reason=reason or None,
                         refills_left=after.refills_left, blocked=after.blocked)
        return md

    # reconciliation ------------------------------------------------------------
    def patient_medication(self, patient_id: str) -> tuple[list[dict], list[dict], list[dict]]:
        p = f"Patient/{patient_id}"
        reqs = self.store.search("MedicationRequest", patient=p)
        disp = self.store.search("MedicationDispense", patient=p)
        stmts = self.store.search("MedicationStatement", patient=p)
        return reqs, disp, stmts

    def reconcile(self, patient_id: str) -> list[reconcile_mod.Flag]:
        flags = reconcile_mod.reconcile(self.cat, *self.patient_medication(patient_id), today=self.today)
        for f in flags:
            self.flags.seen(f, patient_id)
        return flags

    def resolve_flag(self, flag_id: str, actor: str, outcome: str, note: str) -> dict:
        if not note.strip():
            raise DispenseRefused("A resolution needs a note (the evidence you used).")
        rec = self.flags.resolve(flag_id, actor, outcome, note.strip())
        self.audit.write(actor, "reconciliation.resolve", flag=flag_id, flag_type=rec["type"],
                         outcome=outcome, note=note.strip())
        return rec

    def add_statement(self, patient_id: str, product_code: str | None, free_text: str, dose_value: float | None,
                      dose_unit: str, taking: bool, actor: str) -> dict:
        product = self.cat.by_code(product_code or "")
        if product:
            concept = self.cat.concept(product)
        elif free_text.strip():
            concept = {"text": free_text.strip()}
        else:
            raise DispenseRefused("Choose a product or type the medication name.")
        st = {
            "resourceType": "MedicationStatement",
            "status": "active" if taking else "not-taken",
            "medicationCodeableConcept": concept,
            "subject": {"reference": f"Patient/{patient_id}"},
            "dateAsserted": now_iso(),
            "informationSource": {"display": "Patient (reported to " + (actor or "pharmacist") + ")"},
        }
        if dose_value:
            st["dosage"] = [{"doseAndRate": [{"doseQuantity": {"value": dose_value, "unit": dose_unit or "mg"}}]}]
        st = self.store.create(st)
        self.audit.write(actor or "pharmacist", "statement.add", statement=f"MedicationStatement/{st['id']}",
                         patient=f"Patient/{patient_id}")
        return st
