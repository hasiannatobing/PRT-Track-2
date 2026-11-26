"""FHIR access for the pharmacy simulator.

Two stores with the same three methods (read, search, create):

- HttpFhirStore: a real FHIR R4 server (EMR-V2 once the FHIR additions are in,
  or a HAPI FHIR server for development).
- MemoryFhirStore: an in-process store, optionally saved to a JSON file, used for
  the demo and the tests.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class FhirError(Exception):
    pass


def ref_id(reference: str | None) -> str | None:
    """'Patient/123' -> '123'."""
    if not reference:
        return None
    return reference.rsplit("/", 1)[-1]


def _matches(resource: dict, params: dict) -> bool:
    for key, value in params.items():
        if value is None:
            continue
        value = str(value)
        if key == "patient":
            subject = resource.get("subject") or resource.get("patient") or {}
            if ref_id(subject.get("reference")) != ref_id(value):
                return False
        elif key == "status":
            if resource.get("status") not in value.split(","):
                return False
        elif key == "prescription":
            refs = [ref_id(r.get("reference")) for r in resource.get("authorizingPrescription", [])]
            if ref_id(value) not in refs:
                return False
        elif key == "_id":
            if resource.get("id") != value:
                return False
        else:
            raise FhirError(f"search parameter not supported by MemoryFhirStore: {key}")
    return True


class MemoryFhirStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict]] = {}
        if path and path.exists():
            self._data = json.loads(path.read_text())

    def is_empty(self) -> bool:
        return not any(self._data.values())

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1))
            tmp.replace(self.path)

    def read(self, rtype: str, rid: str) -> dict:
        try:
            return json.loads(json.dumps(self._data[rtype][rid]))
        except KeyError:
            raise FhirError(f"{rtype}/{rid} not found") from None

    def search(self, rtype: str, **params) -> list[dict]:
        items = self._data.get(rtype, {}).values()
        return [json.loads(json.dumps(r)) for r in items if _matches(r, params)]

    def create(self, resource: dict) -> dict:
        with self._lock:
            resource = json.loads(json.dumps(resource))
            resource.setdefault("id", uuid.uuid4().hex[:12])
            self._data.setdefault(resource["resourceType"], {})[resource["id"]] = resource
            self._save()
        return resource

    def put(self, resource: dict) -> dict:
        """Create or replace with a fixed id (used by the demo loader)."""
        with self._lock:
            self._data.setdefault(resource["resourceType"], {})[resource["id"]] = resource
            self._save()
        return resource


class HttpFhirStore:
    def __init__(self, base_url: str, token: str | None = None, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        headers = {"Accept": "application/fhir+json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/fhir+json"
            data = json.dumps(body).encode()
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise FhirError(f"{method} {url} -> HTTP {e.code}: {e.read()[:500]!r}") from None
        except urllib.error.URLError as e:
            raise FhirError(f"{method} {url} -> {e.reason}") from None
        return json.loads(raw) if raw else {}

    def read(self, rtype: str, rid: str) -> dict:
        return self._call("GET", f"{self.base}/{rtype}/{urllib.parse.quote(rid)}")

    def search(self, rtype: str, **params) -> list[dict]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base}/{rtype}" + (f"?{query}" if query else "")
        out: list[dict] = []
        for _ in range(50):  # page limit
            bundle = self._call("GET", url)
            out += [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
            nxt = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not nxt:
                break
            url = nxt
        return out

    def create(self, resource: dict) -> dict:
        return self._call("POST", f"{self.base}/{resource['resourceType']}", resource)

    def put(self, resource: dict) -> dict:
        return self._call("PUT", f"{self.base}/{resource['resourceType']}/{resource['id']}", resource)
