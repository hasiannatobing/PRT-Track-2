"""Evaluation metrics (proposal slide 9): sensitivity, false alerts, time to resolve.

Runs the allergy check and reconciliation on the demo patients and compares the
result with demo.EXPECTED_ALLERGY and demo.EXPECTED_FLAGS. Time to resolve comes
from the resolutions pharmacists record in the UI (state/flags.json).
"""
from __future__ import annotations

from datetime import datetime
from statistics import median

from . import demo


def _hours(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def compute(service) -> dict:
    # Reconciliation
    tp = fp = fn = 0
    false_alerts: list[str] = []
    missed: list[str] = []
    for pid, expected in demo.EXPECTED_FLAGS.items():
        got = {(f.type, tuple(sorted(f.evidence))) for f in service.reconcile(pid)}
        tp += len(got & expected)
        fp += len(got - expected)
        fn += len(expected - got)
        false_alerts += [f"{pid}: {t} {e}" for t, e in sorted(got - expected)]
        missed += [f"{pid}: {t} {e}" for t, e in sorted(expected - got)]

    # Allergy check: an alert = warn or review
    a_tp = a_fp = a_fn = 0
    a_wrong: list[str] = []
    for rid, expected in demo.EXPECTED_ALLERGY.items():
        got = service.review(rid).allergy.result
        if got != expected:
            a_wrong.append(f"{rid}: expected {expected}, got {got}")
        alert_e, alert_g = expected != "none", got != "none"
        a_tp += alert_e and alert_g
        a_fp += alert_g and not alert_e
        a_fn += alert_e and not alert_g

    # Time to resolve
    times = [_hours(v["first_seen"], v["resolution"]["at"])
             for v in service.flags.data.values() if v.get("resolution")]

    def sens(t, f):
        return round(t / (t + f), 3) if (t + f) else None

    return {
        "reconciliation": {"true_flags": tp, "false_alerts": fp, "missed": fn, "sensitivity": sens(tp, fn),
                           "false_alert_list": false_alerts, "missed_list": missed},
        "allergy": {"true_alerts": a_tp, "false_alerts": a_fp, "missed": a_fn, "sensitivity": sens(a_tp, a_fn),
                    "wrong_results": a_wrong},
        "time_to_resolve_hours": {"resolved": len(times),
                                  "median": round(median(times), 2) if times else None,
                                  "max": round(max(times), 2) if times else None},
    }
