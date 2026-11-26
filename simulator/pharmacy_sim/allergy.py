"""Drug-allergy check at the dispense step (proposal slide 8).

Compares the product about to be dispensed with the patient's active medication
AllergyIntolerance records:

    Recorded allergy                Product                          Result
    Penicillins (class)             Amoxicillin 500 mg               warn
    Amoxicillin (substance)         Amoxicillin + clavulanic acid    warn
    Penicillins (class)             Azithromycin                     none
    "Antibiotic allergy" free text  any antibiotic                   review

A *class* allergy matches by ATC prefix, a *substance* allergy by ingredient.
A vague or unrecognised medication allergy cannot be matched safely, so it goes
to pharmacist review. A warning must be overridden with a reason; a review
must be confirmed with a note. Both are stored on the MedicationDispense.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalogue import Catalogue, Product, has_word, norm

ORDER = {"none": 0, "review": 1, "warn": 2}


@dataclass
class AllergyFinding:
    result: str            # warn | review
    allergy_id: str | None
    allergy_text: str
    reason: str


@dataclass
class AllergyCheck:
    result: str = "none"   # none | review | warn
    findings: list[AllergyFinding] = field(default_factory=list)

    @property
    def needs_action(self) -> bool:
        return self.result != "none"

    def summary(self) -> str:
        if not self.findings:
            return "No allergy alert."
        return " ".join(f.reason for f in self.findings)


def _is_active(allergy: dict) -> bool:
    codes = [c.get("code") for c in (allergy.get("clinicalStatus") or {}).get("coding", [])]
    return not codes or "active" in codes


def _is_medication(allergy: dict) -> bool:
    cats = allergy.get("category") or []
    return not cats or "medication" in cats


def _allergy_text(allergy: dict) -> str:
    code = allergy.get("code") or {}
    return code.get("text") or next((c.get("display") for c in code.get("coding", []) if c.get("display")), "")


def classify_allergy(catalogue: Catalogue, text: str) -> dict | None:
    """Match an allergy's wording to a catalogue allergen; substance and class before vague."""
    t = norm(text)
    ranked = sorted(catalogue.allergens, key=lambda a: {"substance": 0, "class": 1, "vague": 2}[a["kind"]])
    for allergen in ranked:
        if any(has_word(t, n) for n in allergen["names"]):
            return allergen
    return None


def check(catalogue: Catalogue, product: Product | None, product_text: str, allergies: list[dict]) -> AllergyCheck:
    out = AllergyCheck()
    for allergy in allergies:
        if not (_is_active(allergy) and _is_medication(allergy)):
            continue
        text = _allergy_text(allergy)
        allergen = classify_allergy(catalogue, text)
        finding: AllergyFinding | None = None

        if product is None:
            finding = AllergyFinding("review", allergy.get("id"), text,
                                     f"'{product_text}' is not in the catalogue, so it cannot be checked "
                                     f"against the allergy '{text}'. Pharmacist review.")
        elif allergen is None:
            finding = AllergyFinding("review", allergy.get("id"), text,
                                     f"Allergy '{text}' is not coded or not recognised. Pharmacist review.")
        elif allergen["kind"] == "substance":
            hit = set(allergen["ingredients"]) & set(product.ingredients)
            if hit:
                finding = AllergyFinding("warn", allergy.get("id"), text,
                                         f"{product.display} contains {', '.join(sorted(hit))}; "
                                         f"recorded allergy: {text}.")
        elif allergen["kind"] == "class":
            if any(product.atc.startswith(p) for p in allergen["atc_prefixes"]):
                finding = AllergyFinding("warn", allergy.get("id"), text,
                                         f"{product.display} (ATC {product.atc}) belongs to the class "
                                         f"{allergen['label']}; recorded allergy: {text}.")
        elif allergen["kind"] == "vague":
            if any(product.atc.startswith(p) for p in allergen["atc_prefixes"]):
                finding = AllergyFinding("review", allergy.get("id"), text,
                                         f"Allergy '{text}' is too unspecific to match "
                                         f"{product.display}. Pharmacist review.")

        if finding:
            out.findings.append(finding)
            if ORDER[finding.result] > ORDER[out.result]:
                out.result = finding.result
    return out
