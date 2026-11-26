"""Product, allergen and duplicate-group lookups, plus small FHIR field helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"


def norm(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9.+ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def has_word(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) is not None


@dataclass(frozen=True)
class Product:
    code: str
    display: str
    atc: str
    ingredients: tuple[str, ...]

    @property
    def key(self) -> str:
        """Same key = same drug for reconciliation, whatever the strength."""
        return "+".join(sorted(self.ingredients))


@dataclass
class Catalogue:
    product_system: str
    products: list[Product]
    allergens: list[dict]
    duplicate_groups: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Catalogue":
        raw = json.loads(Path(path).read_text())
        products = [
            Product(p["code"], p["display"], p["atc"], tuple(p["ingredients"])) for p in raw["products"]
        ]
        return cls(raw["product_system"], products, raw["allergens"], raw.get("duplicate_groups", []))

    def by_code(self, code: str) -> Product | None:
        return next((p for p in self.products if p.code == code), None)

    def product_for(self, concept: dict | None) -> Product | None:
        """Find the product for a CodeableConcept: by code first, then by display text."""
        concept = concept or {}
        for coding in concept.get("coding", []):
            p = self.by_code(coding.get("code") or "")
            if p:
                return p
        texts = [concept.get("text")] + [c.get("display") for c in concept.get("coding", [])]
        for text in filter(None, texts):
            t = norm(text)
            for p in self.products:
                if norm(p.display) == t:
                    return p
        return None

    def concept(self, product: Product) -> dict:
        return {
            "coding": [{"system": self.product_system, "code": product.code, "display": product.display},
                       {"system": "http://www.whocc.no/atc", "code": product.atc}],
            "text": product.display,
        }

    def duplicate_group(self, product: Product | None) -> dict | None:
        if not product:
            return None
        return next((g for g in self.duplicate_groups if product.atc.startswith(g["atc_prefix"])), None)


# --- FHIR field helpers -------------------------------------------------------

def med_text(resource: dict) -> str:
    concept = resource.get("medicationCodeableConcept") or {}
    if concept.get("text"):
        return concept["text"]
    return next((c.get("display") for c in concept.get("coding", []) if c.get("display")), "Unknown medication")


def dose_of(resource: dict) -> tuple[float, str] | None:
    """First doseQuantity on MedicationRequest.dosageInstruction or MedicationStatement.dosage."""
    dosages = resource.get("dosageInstruction") or resource.get("dosage") or []
    for d in dosages:
        for dr in d.get("doseAndRate", []):
            q = dr.get("doseQuantity")
            if q and q.get("value") is not None:
                return float(q["value"]), q.get("unit") or q.get("code") or ""
    return None


def strength_mg(product: Product | None) -> float | None:
    """First 'NNN mg' in a product name, used to turn tablets into mg."""
    if not product:
        return None
    m = re.search(r"([\d.]+)\s*mg", product.display.lower())
    return float(m.group(1)) if m else None


def dose_mg(resource: dict, product: Product | None) -> float | None:
    dose = dose_of(resource)
    if not dose:
        return None
    value, unit = dose
    unit = unit.lower()
    if unit == "mg":
        return value
    if unit == "g":
        return value * 1000
    if unit in ("tablet", "tablets", "tab", "capsule", "capsules", "cap", "{tbl}", "1"):
        s = strength_mg(product)
        return value * s if s else None
    return None
