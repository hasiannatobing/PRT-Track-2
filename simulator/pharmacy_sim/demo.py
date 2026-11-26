"""Synthetic demo patients, each built to show one scenario from slides 8 and 9.

All names and data are invented. Dates are relative to `today` so the gap flags
behave the same whenever the demo is loaded.

EXPECTED holds the right answers, so metrics.py can measure sensitivity and
false alerts against them.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .catalogue import Catalogue


def _ts(d: date) -> str:
    return datetime.combine(d, time(9, 0), tzinfo=timezone.utc).isoformat()


def build(cat: Catalogue, today: date) -> list[dict]:
    out: list[dict] = []

    def patient(pid, given, family, birth):
        out.append({"resourceType": "Patient", "id": pid, "name": [{"given": [given], "family": family}],
                    "birthDate": birth, "identifier": [{"system": "urn:fhtw:mrn", "value": pid.upper()}]})

    def allergy(aid, pid, text, criticality="high"):
        out.append({
            "resourceType": "AllergyIntolerance", "id": aid,
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                           "code": "active"}]},
            "category": ["medication"], "criticality": criticality,
            "code": {"text": text}, "patient": {"reference": f"Patient/{pid}"},
        })

    def rx(rid, pid, code, qty, repeats, days_ago, dose=None, dose_unit=None, supply_days=30, status="active"):
        p = cat.by_code(code)
        r = {
            "resourceType": "MedicationRequest", "id": rid, "status": status, "intent": "order",
            "medicationCodeableConcept": cat.concept(p),
            "subject": {"reference": f"Patient/{pid}"},
            "authoredOn": _ts(today - timedelta(days=days_ago)),
            "requester": {"display": "Dr. Demo Prescriber"},
            "dispenseRequest": {
                "numberOfRepeatsAllowed": repeats,
                "quantity": {"value": qty, "unit": "tablets"},
                "expectedSupplyDuration": {"value": supply_days, "unit": "days",
                                           "system": "http://unitsofmeasure.org", "code": "d"},
            },
        }
        if dose:
            r["dosageInstruction"] = [{"text": f"{dose} {dose_unit} once daily",
                                       "doseAndRate": [{"doseQuantity": {"value": dose, "unit": dose_unit}}]}]
        out.append(r)

    def md(did, rid, pid, code, qty, days_ago, kind="RF"):
        p = cat.by_code(code)
        out.append({
            "resourceType": "MedicationDispense", "id": did, "status": "completed",
            "medicationCodeableConcept": cat.concept(p),
            "subject": {"reference": f"Patient/{pid}"},
            "authorizingPrescription": [{"reference": f"MedicationRequest/{rid}"}],
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": kind}]},
            "quantity": {"value": qty, "unit": "tablets"},
            "whenHandedOver": _ts(today - timedelta(days=days_ago)),
            "performer": [{"actor": {"display": "Demo Pharmacist"}}],
            "substitution": {"wasSubstituted": False},
        })

    def stmt(sid, pid, code, dose=None, dose_unit="mg", status="active", text=None):
        concept = cat.concept(cat.by_code(code)) if code else {"text": text}
        s = {"resourceType": "MedicationStatement", "id": sid, "status": status,
             "medicationCodeableConcept": concept, "subject": {"reference": f"Patient/{pid}"},
             "dateAsserted": _ts(today - timedelta(days=1)), "informationSource": {"display": "Patient"}}
        if dose:
            s["dosage"] = [{"doseAndRate": [{"doseQuantity": {"value": dose, "unit": dose_unit}}]}]
        out.append(s)

    # Refills: two fills done, third is overdue (timeline gap) -> 2 refills left.
    patient("p-ben", "Ben", "Refill", "1961-03-14")
    rx("rx-ben-rami", "p-ben", "RAMI5", 30, 3, 70, 5, "mg")
    md("md-ben-1", "rx-ben-rami", "p-ben", "RAMI5", 30, 69, "FF")
    md("md-ben-2", "rx-ben-rami", "p-ben", "RAMI5", 30, 38, "RF")

    # Partial fill: 20 of 30 in stock -> fill 1 in progress, 3 refills left.
    patient("p-hana", "Hana", "Partial", "1990-07-02")
    rx("rx-hana-met", "p-hana", "MET500", 30, 3, 5, 500, "mg")
    md("md-hana-1", "rx-hana-met", "p-hana", "MET500", 20, 4, "FFP")

    # Exhausted: all 4 fills used -> blocked.
    patient("p-greta", "Greta", "Exhausted", "1955-11-30")
    rx("rx-greta-panto", "p-greta", "PANTO40", 30, 3, 125, 40, "mg")
    for i, days in enumerate((124, 94, 63, 32)):
        md(f"md-greta-{i + 1}", "rx-greta-panto", "p-greta", "PANTO40", 30, days, "FF" if i == 0 else "RF")

    # Allergy rows from slide 8.
    patient("p-clara", "Clara", "Allergy", "1984-05-21")
    allergy("al-clara-pen", "p-clara", "Penicillins (class)")
    rx("rx-clara-amox", "p-clara", "AMOX500", 21, 0, 1, 500, "mg", supply_days=7)
    rx("rx-clara-azi", "p-clara", "AZI250", 6, 0, 1, 250, "mg", supply_days=5)

    patient("p-david", "David", "Substance", "1977-09-09")
    allergy("al-david-amox", "p-david", "Amoxicillin (substance)")
    rx("rx-david-amoxclav", "p-david", "AMOXCLAV875", 14, 0, 1, 875, "mg", supply_days=7)

    patient("p-eva", "Eva", "Freetext", "1999-01-17")
    allergy("al-eva-abx", "p-eva", "Antibiotic allergy", "low")
    rx("rx-eva-azi", "p-eva", "AZI250", 6, 0, 1, 250, "mg", supply_days=5)

    # Reconciliation rows from slide 9.
    patient("p-felix", "Felix", "Recon", "1968-12-03")
    rx("rx-felix-met", "p-felix", "MET1000", 60, 2, 20, 1000, "mg")
    md("md-felix-met-1", "rx-felix-met", "p-felix", "MET1000", 60, 19, "FF")
    rx("rx-felix-ibu", "p-felix", "IBU400", 20, 0, 10, 400, "mg", supply_days=10)
    stmt("st-felix-met", "p-felix", "MET500", 500)          # dose mismatch: ordered 1000 mg, says 500 mg
    stmt("st-felix-para", "p-felix", "PARA500", 1000)       # unrecorded OTC
    stmt("st-felix-diclo", "p-felix", "DICLO50", 50)        # unrecorded + duplicate NSAID with ibuprofen

    return out


# The right answers for metrics.py ------------------------------------------------

EXPECTED_ALLERGY = {
    # request id -> expected result of the allergy check
    "rx-clara-amox": "warn",
    "rx-clara-azi": "none",
    "rx-david-amoxclav": "warn",
    "rx-eva-azi": "review",
    "rx-ben-rami": "none",
    "rx-felix-met": "none",
    "rx-felix-ibu": "none",
}

EXPECTED_FLAGS = {
    # patient id -> set of (flag type, sorted evidence) that a perfect check raises
    "p-felix": {
        ("dose_mismatch", ("MedicationRequest/rx-felix-met", "MedicationStatement/st-felix-met")),
        ("not_dispensed_not_taken", ("MedicationRequest/rx-felix-ibu",)),
        ("unrecorded_medication", ("MedicationStatement/st-felix-para",)),
        ("unrecorded_medication", ("MedicationStatement/st-felix-diclo",)),
        ("duplicate_therapy", ("MedicationRequest/rx-felix-ibu", "MedicationStatement/st-felix-diclo")),
    },
    "p-ben": set(),
    "p-hana": set(),
    "p-greta": set(),
    "p-clara": set(),
    "p-david": set(),
    "p-eva": set(),
}


def load(store, cat: Catalogue, today: date) -> int:
    resources = build(cat, today)
    for r in resources:
        store.put(r)
    return len(resources)
