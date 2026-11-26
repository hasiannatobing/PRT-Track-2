"""Tests built from the proposal's own tables (slides 8 and 9).

Run from track2-medication/simulator:   python -m unittest -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pharmacy_sim import demo, metrics  # noqa: E402
from pharmacy_sim.allergy import check  # noqa: E402
from pharmacy_sim.catalogue import Catalogue  # noqa: E402
from pharmacy_sim.fhir import MemoryFhirStore  # noqa: E402
from pharmacy_sim.refills import refill_status  # noqa: E402
from pharmacy_sim.service import DispenseRefused, PharmacyService  # noqa: E402

TODAY = date(2026, 11, 23)
CAT = Catalogue.load()


def request(qty=30, repeats=3):
    return {"resourceType": "MedicationRequest", "id": "rx", "status": "active",
            "dispenseRequest": {"quantity": {"value": qty, "unit": "tablets"}, "numberOfRepeatsAllowed": repeats}}


def disp(qty, n, status="completed"):
    return {"id": f"d{n}", "status": status, "quantity": {"value": qty}, "whenHandedOver": f"2026-01-{n:02d}T09:00:00+00:00"}


class RefillRule(unittest.TestCase):
    """Slide 8, left: 30 tablets + 3 repeats = 4 fills."""

    def test_first_fill_leaves_3(self):
        self.assertEqual(refill_status(request(), [disp(30, 1)]).refills_left, 3)

    def test_second_fill_leaves_2(self):
        self.assertEqual(refill_status(request(), [disp(30, 1), disp(30, 2)]).refills_left, 2)

    def test_partial_fill_unchanged(self):
        st = refill_status(request(), [disp(30, 1), disp(20, 2)])
        self.assertEqual(st.refills_left, 3)
        self.assertEqual(st.max_next_quantity, 10)
        st = refill_status(request(), [disp(30, 1), disp(20, 2), disp(10, 3)])
        self.assertEqual(st.refills_left, 2)

    def test_fill_after_last_repeat_blocked(self):
        st = refill_status(request(), [disp(30, i) for i in range(1, 5)])
        self.assertEqual(st.refills_left, 0)
        self.assertTrue(st.blocked)
        self.assertEqual(st.fills_allowed, 4)

    def test_on_hold_does_not_count(self):
        self.assertEqual(refill_status(request(), [disp(30, 1, "on-hold")]).fills_completed, 0)

    def test_no_quantity_counts_each_dispense_as_a_fill(self):
        r = request()
        del r["dispenseRequest"]["quantity"]
        st = refill_status(r, [disp(5, 1)])
        self.assertFalse(st.quantity_known)
        self.assertEqual(st.fills_completed, 1)


class AllergyRule(unittest.TestCase):
    """Slide 8, right."""

    def run_check(self, allergy_text, code):
        allergy = {"id": "a", "clinicalStatus": {"coding": [{"code": "active"}]}, "category": ["medication"],
                   "code": {"text": allergy_text}}
        p = CAT.by_code(code)
        return check(CAT, p, p.display, [allergy]).result

    def test_class_vs_product_warns(self):
        self.assertEqual(self.run_check("Penicillins (class)", "AMOX500"), "warn")

    def test_substance_vs_combination_warns(self):
        self.assertEqual(self.run_check("Amoxicillin (substance)", "AMOXCLAV875"), "warn")

    def test_class_vs_other_class_no_alert(self):
        self.assertEqual(self.run_check("Penicillins (class)", "AZI250"), "none")

    def test_free_text_goes_to_review(self):
        self.assertEqual(self.run_check("Antibiotic allergy", "AZI250"), "review")

    def test_unrecognised_text_goes_to_review(self):
        self.assertEqual(self.run_check("reacted badly to the pink pills", "RAMI5"), "review")

    def test_inactive_allergy_ignored(self):
        allergy = {"clinicalStatus": {"coding": [{"code": "resolved"}]}, "code": {"text": "Penicillins"}}
        p = CAT.by_code("AMOX500")
        self.assertEqual(check(CAT, p, p.display, [allergy]).result, "none")


class Workflow(unittest.TestCase):
    """Slide 7 workflow on the demo data, through the service."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = MemoryFhirStore()
        demo.load(store, CAT, TODAY)
        self.s = PharmacyService(store, CAT, Path(self.tmp.name), today=TODAY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_dispense_is_linked_and_audited(self):
        md = self.s.dispense("rx-ben-rami", "full", "Pharm A")
        self.assertEqual(md["authorizingPrescription"][0]["reference"], "MedicationRequest/rx-ben-rami")
        self.assertEqual(md["quantity"]["value"], 30)
        self.assertEqual(md["type"]["coding"][0]["code"], "RF")
        self.assertEqual(self.s.review("rx-ben-rami").status.refills_left, 1)
        self.assertEqual(self.s.audit.entries()[-1]["action"], "dispense.full")

    def test_completing_a_partial_fill(self):
        md = self.s.dispense("rx-hana-met", "full", "Pharm A")
        self.assertEqual(md["quantity"]["value"], 10)
        self.assertEqual(md["type"]["coding"][0]["code"], "FFC")

    def test_partial_cannot_exceed_or_complete(self):
        with self.assertRaises(DispenseRefused):
            self.s.dispense("rx-hana-met", "partial", "Pharm A", 15)
        with self.assertRaises(DispenseRefused):
            self.s.dispense("rx-hana-met", "partial", "Pharm A", 10)
        self.s.dispense("rx-hana-met", "partial", "Pharm A", 4)
        self.assertEqual(self.s.review("rx-hana-met").status.refills_left, 3)

    def test_exhausted_is_blocked_and_refusal_audited(self):
        with self.assertRaises(DispenseRefused) as ctx:
            self.s.dispense("rx-greta-panto", "full", "Pharm A")
        self.assertIn("Blocked", str(ctx.exception))
        self.assertEqual(self.s.audit.entries()[-1]["action"], "dispense.refused")

    def test_hold_needs_reason_and_does_not_count(self):
        with self.assertRaises(DispenseRefused):
            self.s.dispense("rx-ben-rami", "hold", "Pharm A")
        md = self.s.dispense("rx-ben-rami", "hold", "Pharm A", reason="Prescriber to confirm dose")
        self.assertEqual(md["status"], "on-hold")
        self.assertEqual(self.s.review("rx-ben-rami").status.refills_left, 2)

    def test_substitute_different_strength(self):
        with self.assertRaises(DispenseRefused):
            self.s.dispense("rx-ben-rami", "substitute", "Pharm A", 15, "RAMI2.5")
        md = self.s.dispense("rx-ben-rami", "substitute", "Pharm A", 15, "RAMI2.5", reason="5 mg out of stock")
        self.assertTrue(md["substitution"]["wasSubstituted"])
        self.assertEqual(md["medicationCodeableConcept"]["coding"][0]["code"], "RAMI2.5")
        # The substitution does not use up a repeat of its own (slide 8: "unchanged").
        self.assertEqual(self.s.review("rx-ben-rami").status.refills_left, 2)

    def test_allergy_warning_needs_override_reason(self):
        with self.assertRaises(DispenseRefused) as ctx:
            self.s.dispense("rx-clara-amox", "full", "Pharm A")
        self.assertIn("Allergy warning", str(ctx.exception))
        md = self.s.dispense("rx-clara-amox", "full", "Pharm A", allergy_note="Tolerated amoxicillin in 2025, prescriber confirmed")
        issue = md["contained"][0]
        self.assertEqual(issue["resourceType"], "DetectedIssue")
        self.assertTrue(issue["mitigation"][0]["action"]["text"].startswith("Overridden"))

    def test_no_alert_dispenses_directly(self):
        md = self.s.dispense("rx-clara-azi", "full", "Pharm A")
        self.assertNotIn("contained", md)

    def test_timeline_flags(self):
        self.assertEqual([g.kind for g in self.s.review("rx-ben-rami").timeline.gaps], ["overdue_refill"])
        self.assertEqual([g.kind for g in self.s.review("rx-greta-panto").timeline.gaps], ["exhausted"])
        self.assertEqual(self.s.review("rx-hana-met").timeline.gaps, [])

    def test_reconciliation_flags_slide_9(self):
        got = sorted(f.type for f in self.s.reconcile("p-felix"))
        self.assertEqual(got, ["dose_mismatch", "duplicate_therapy", "not_dispensed_not_taken",
                               "unrecorded_medication", "unrecorded_medication"])

    def test_resolution_needs_note_and_sticks(self):
        flag = self.s.reconcile("p-felix")[0]
        with self.assertRaises(DispenseRefused):
            self.s.resolve_flag(flag.id, "Pharm A", "corrected", " ")
        self.s.resolve_flag(flag.id, "Pharm A", "corrected", "Called patient")
        again = next(f for f in self.s.reconcile("p-felix") if f.id == flag.id)
        self.assertEqual(again.resolution["by"], "Pharm A")

    def test_metrics_on_demo_set(self):
        m = metrics.compute(self.s)
        self.assertEqual(m["reconciliation"]["sensitivity"], 1.0)
        self.assertEqual(m["reconciliation"]["false_alerts"], 0)
        self.assertEqual(m["allergy"]["wrong_results"], [])


if __name__ == "__main__":
    unittest.main()
