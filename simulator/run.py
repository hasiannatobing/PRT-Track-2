#!/usr/bin/env python3
"""Pharmacy simulator for FHTW-EMR Track 2.

Usage:
    python run.py serve                  # demo mode: in-memory FHIR store with synthetic patients
    python run.py serve --fhir URL       # against a FHIR R4 server (EMR-V2 or HAPI FHIR)
    python run.py load-demo --fhir URL   # PUT the synthetic patients into a FHIR server
    python run.py metrics                # sensitivity / false alerts on the demo set
    python run.py reset                  # forget demo data, audit trail and flag resolutions

Environment: FHIR_BASE_URL, FHIR_TOKEN (bearer token), STATE_DIR (default ./state).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pharmacy_sim import demo, metrics  # noqa: E402
from pharmacy_sim.catalogue import Catalogue  # noqa: E402
from pharmacy_sim.fhir import HttpFhirStore, MemoryFhirStore  # noqa: E402
from pharmacy_sim.service import PharmacyService  # noqa: E402
from pharmacy_sim.web import serve  # noqa: E402


def build(args) -> PharmacyService:
    state = Path(args.state)
    cat = Catalogue.load()
    if args.fhir:
        store = HttpFhirStore(args.fhir, os.environ.get("FHIR_TOKEN"))
    else:
        store = MemoryFhirStore(state / "fhir-store.json")
        if store.is_empty():
            n = demo.load(store, cat, date.today())
            print(f"Loaded {n} synthetic FHIR resources into the demo store.")
    return PharmacyService(store, cat, state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["serve", "load-demo", "metrics", "reset"])
    ap.add_argument("--fhir", default=os.environ.get("FHIR_BASE_URL"), help="FHIR R4 base URL (default: demo store)")
    ap.add_argument("--state", default=os.environ.get("STATE_DIR", str(HERE / "state")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8095)
    args = ap.parse_args()

    if args.command == "reset":
        shutil.rmtree(args.state, ignore_errors=True)
        print(f"Removed {args.state}")
        return
    if args.command == "load-demo":
        if not args.fhir:
            sys.exit("load-demo needs --fhir (the demo store loads itself).")
        store = HttpFhirStore(args.fhir, os.environ.get("FHIR_TOKEN"))
        print(f"PUT {demo.load(store, Catalogue.load(), date.today())} resources to {args.fhir}")
        return

    service = build(args)
    if args.command == "metrics":
        print(json.dumps(metrics.compute(service), indent=2))
    else:
        serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
