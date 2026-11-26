# Track 2 · Medication: pharmacy simulator

`MedicationRequest` in, `MedicationDispense` out, with the two safety checks from the proposal at the dispense step (refill limit and drug–allergy), a prescribed-vs-dispensed timeline, and reconciliation.

Needs only Python 3.10+ (standard library, no packages). All data is synthetic.

```bash
cd track2-medication/simulator
python run.py serve                    # open http://127.0.0.1:8095
python -m unittest discover -s tests   # 24 tests built from slides 8 and 9
python run.py metrics                  # sensitivity / false alerts on the demo set
python run.py reset                    # start the demo over
```

## How it fits the plan

We use the hybrid approach: EMR-V2 gets a small FHIR-only change (see [../docs/track2-medication/emr-v2-findings.md](../docs/track2-medication/emr-v2-findings.md)), and every pharmacy rule lives here. Until the EMR-V2 change is merged, the simulator runs in **demo mode** on an in-memory FHIR store. Pointing it at a FHIR server is one flag:

```bash
python run.py serve --fhir http://localhost:8090/fhir/R4     # EMR-V2, once it has the FHIR additions
FHIR_TOKEN=... python run.py serve --fhir ...                # bearer token if the server needs one
```

| Simulator reads | Simulator writes |
|---|---|
| `MedicationRequest` (needs `dispenseRequest.quantity`, `numberOfRepeatsAllowed`, optional `expectedSupplyDuration`) | `MedicationDispense` (completed or on-hold, `authorizingPrescription`, `substitution`, contained `DetectedIssue` for allergy overrides) |
| `AllergyIntolerance?patient=` | `MedicationStatement` (what the patient says they take) |
| `MedicationDispense?prescription=`, `MedicationStatement?patient=`, `Patient/{id}` | |

## The workflow

1. **Worklist** lists active prescriptions with refills left, allergy result and timeline flags.
2. **Review** one order: refill status, allergy check, timeline, history.
3. **Dispense**: full (completes the current fill), partial, hold (needs a reason), or substitute (needs a product and a reason).
4. Each action creates a linked `MedicationDispense`.
5. Every action and every refusal goes to the **audit trail** (`state/audit.jsonl`).

## Rules

**Refill limit** (`pharmacy_sim/refills.py`). Repeats exclude the original dispense: 30 tablets + 3 repeats = 4 fills. A fill is complete when its dispenses add up to the prescribed quantity. Partial fills leave the count unchanged until the fill is complete. After the last fill, further dispenses are blocked. The count is always recomputed from the dispense history, so the blocking rule, the timeline and reconciliation agree.

**Allergy check** (`pharmacy_sim/allergy.py`, data in `data/catalogue.json`). A class allergy matches by ATC prefix (penicillins = `J01C`), and a substance allergy matches by ingredient. Vague or uncoded text goes to pharmacist review. A warning can only be passed with an override reason, and a review only with a note. Both are stored on the dispense.

| Recorded allergy | Product | Result |
|---|---|---|
| Penicillins (class) | Amoxicillin 500 mg | warn |
| Amoxicillin (substance) | Amoxicillin + clavulanic acid | warn |
| Penicillins (class) | Azithromycin | no alert |
| "Antibiotic allergy" (free text) | Azithromycin | review |

**Timeline** (`pharmacy_sim/timeline.py`) flags these cases:
- An active order with no dispense after 3 days.
- A refill overdue after the last fill's supply ran out.
- All fills used.

**Reconciliation** (`pharmacy_sim/reconcile.py`) compares the order, the supply and the patient's report. It flags dose mismatch, not dispensed and not taken, unrecorded medication (for example OTC) and duplicate therapy (same group in `catalogue.json`). Each flag is resolved with an outcome and a note. Resolution times feed the time-to-resolve metric.

## Demo patients

| Patient | Shows |
|---|---|
| Ben Refill | 2 of 4 fills done, refill overdue (timeline gap) |
| Hana Partial | Partial fill, 20 of 30 tablets |
| Greta Exhausted | All 4 fills used, so further fills are blocked |
| Clara Allergy | Penicillin class allergy: amoxicillin warns, azithromycin passes |
| David Substance | Amoxicillin allergy vs amoxicillin + clavulanic acid |
| Eva Freetext | "Antibiotic allergy", which needs pharmacist review |
| Felix Recon | All four reconciliation flags |

## Open points (need a clinician)

- **Substitution.** The deck says a substitution leaves the refill count "unchanged". We read that as: a substitute counts toward the current fill by its quantity (entered in the prescribed product's units) and never uses up a repeat of its own. This needs confirmation.
- **Allergen set and products.** `data/catalogue.json` is a small teaching set and is not clinically validated.
- **Blocking.** Should any allergy warning be a hard block rather than an override?

## Layout

```
track2-medication/
  docker-compose.yml        optional HAPI FHIR server (not yet tested)
  simulator/
    run.py                  entry point
    data/catalogue.json     products (ATC), allergens, duplicate groups
    pharmacy_sim/
      fhir.py               FHIR client + in-memory store
      service.py            the workflow and its rules, audit trail
      refills.py            refill limit
      allergy.py            drug-allergy check
      timeline.py           prescribed vs dispensed, gaps, SVG
      reconcile.py          reconciliation flags
      metrics.py            sensitivity, false alerts, time to resolve
      demo.py               synthetic patients + expected answers
      web.py                pharmacist UI
    tests/test_rules.py
```

The web UI has no login and is meant for `localhost` only.
