"""Pharmacist web UI (Python standard library only; local use, no login)."""
from __future__ import annotations

import json
import traceback
import urllib.parse
from html import escape as e
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import metrics as metrics_mod
from . import timeline as timeline_mod
from .catalogue import med_text
from .fhir import FhirError, ref_id
from .refills import _fmt
from .service import DispenseRefused, PharmacyService

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#dedcd6;--accent:#1f5fae;
--ok:#2e7d32;--warn:#b26a00;--bad:#b3261e;--rx:#cfe0f5;--fill:#2e7d32;--partial:#e0a100;--gap:#e57373}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#212120;--ink:#ecebe6;--mute:#a3a29c;--line:#3a3936;
--accent:#7fb0ef;--rx:#26425f;--fill:#5fb56a;--partial:#e8b23a;--gap:#c25b5b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}header a{color:var(--accent);text-decoration:none}
main{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 8px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);
vertical-align:top}th{color:var(--mute);font-weight:600;font-size:13px}
a{color:var(--accent)}.mute{color:var(--mute)}.pill{display:inline-block;padding:1px 8px;border-radius:99px;
font-size:12px;border:1px solid currentColor}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.msg{padding:10px 12px;border-radius:8px;margin-bottom:14px;border:1px solid}.msg.ok{background:transparent}
form.grid{display:grid;grid-template-columns:160px 1fr;gap:8px 12px;align-items:center}
input,select,textarea{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
color:var(--ink);width:100%}button{font:inherit;padding:7px 14px;border-radius:6px;border:0;background:var(--accent);
color:#fff;cursor:pointer}.wrap{overflow-x:auto}
svg .rx{fill:var(--rx)}svg .fill{fill:var(--fill)}svg .partial{fill:var(--partial)}svg .gap{fill:var(--gap);opacity:.55}
svg .axis{stroke:var(--line);stroke-width:2}svg .today{stroke:var(--mute);stroke-dasharray:3 3}
svg text{fill:var(--ink)}svg .small{font-size:11px;fill:var(--mute)}svg .lbl{font-size:12px;font-weight:600}
svg .hold{font-size:12px;font-weight:700;fill:var(--warn)}
@media (max-width:640px){form.grid{grid-template-columns:1fr}}
"""


def page(title: str, body: str, msg: str = "", err: str = "") -> bytes:
    flash = ""
    if msg:
        flash += f'<div class="msg ok">{e(msg)}</div>'
    if err:
        flash += f'<div class="msg bad">{e(err)}</div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Pharmacy simulator</title>
<style>{CSS}</style></head><body><header><b>Pharmacy simulator</b><a href="/">Worklist</a><a href="/audit">Audit trail</a>
<a href="/metrics">Metrics</a><span class="mute">Synthetic data only</span></header><main>{flash}{body}</main></body></html>""".encode()


def patient_name(p: dict | None) -> str:
    if not p:
        return "Unknown patient"
    n = (p.get("name") or [{}])[0]
    return " ".join(n.get("given", []) + [n.get("family", "")]).strip() or p.get("id", "")


def allergy_pill(result: str) -> str:
    cls = {"none": "ok", "review": "warn", "warn": "bad"}[result]
    text = {"none": "no alert", "review": "review", "warn": "allergy warning"}[result]
    return f'<span class="pill {cls}">{text}</span>'


class App:
    def __init__(self, service: PharmacyService):
        self.s = service

    # GET / -------------------------------------------------------------------
    def worklist(self, q) -> bytes:
        rows = []
        for rv in sorted(self.s.worklist(), key=lambda r: r.request.get("authoredOn") or "", reverse=True):
            st = rv.status
            gaps = ", ".join(g.kind.replace("_", " ") for g in rv.timeline.gaps) or "—"
            state = '<span class="pill bad">blocked</span>' if st.blocked else e(f"{st.refills_left} left")
            pid = ref_id(rv.request["subject"]["reference"])
            rows.append(f"""<tr><td><a href="/rx/{e(rv.request['id'])}">{e(med_text(rv.request))}</a></td>
<td><a href="/patient/{e(pid)}">{e(patient_name(rv.patient))}</a></td>
<td>{e(_fmt(st.prescribed_quantity))} {e(st.unit)} + {st.repeats_allowed}</td><td>{state}</td>
<td>{allergy_pill(rv.allergy.result)}</td><td>{e(gaps)}</td></tr>""")
        body = f"""<h1>Active prescriptions</h1><div class="card wrap"><table>
<tr><th>Medication</th><th>Patient</th><th>Prescribed</th><th>Refills</th><th>Allergy</th><th>Timeline flags</th></tr>
{''.join(rows) or '<tr><td colspan=6 class="mute">No active MedicationRequest found.</td></tr>'}</table></div>"""
        return page("Worklist", body, q.get("msg", ""), q.get("err", ""))

    # GET /rx/<id> ------------------------------------------------------------
    def request(self, rid: str, q) -> bytes:
        sub = q.get("sub") or None
        rv = self.s.review(rid, sub)
        rx, st = rv.request, rv.status
        pid = ref_id(rx["subject"]["reference"])
        dosage = (rx.get("dosageInstruction") or [{}])[0].get("text", "—")

        gaps = "".join(f'<li class="{"bad" if g.kind != "exhausted" else "warn"}">{e(g.message)}</li>'
                       for g in rv.timeline.gaps)
        findings = "".join(f"<li>{e(f.reason)}</li>" for f in rv.allergy.findings)
        hist = "".join(
            f"<tr><td>{e((d.get('whenHandedOver') or d.get('whenPrepared') or '')[:16].replace('T', ' '))}</td>"
            f"<td>{e(d.get('status'))}</td><td>{e(((d.get('type') or {}).get('coding') or [{}])[0].get('code', ''))}</td>"
            f"<td>{e(_fmt((d.get('quantity') or {}).get('value')))}</td><td>{e(med_text(d))}</td>"
            f"<td>{e(((d.get('performer') or [{}])[0].get('actor') or {}).get('display', ''))}</td></tr>"
            for d in sorted(rv.dispenses, key=lambda d: d.get("whenHandedOver") or d.get("whenPrepared") or ""))
        options = "".join(f'<option value="{e(p.code)}"{" selected" if p.code == sub else ""}>{e(p.display)}</option>'
                          for p in self.s.cat.products)
        remaining = "" if st.max_next_quantity is None else f"{_fmt(st.max_next_quantity)} {st.unit}"

        body = f"""<h1>{e(med_text(rx))}</h1>
<div class="card"><table>
<tr><th>Patient</th><td><a href="/patient/{e(pid)}">{e(patient_name(rv.patient))}</a></td></tr>
<tr><th>Order</th><td>MedicationRequest/{e(rx['id'])} · {e(rx.get('status'))} · written {e((rx.get('authoredOn') or '')[:10])}</td></tr>
<tr><th>Dosage</th><td>{e(dosage)}</td></tr>
<tr><th>Prescribed</th><td>{e(_fmt(st.prescribed_quantity))} {e(st.unit)} + {st.repeats_allowed} repeats = {st.fills_allowed} fill{"s" if st.fills_allowed != 1 else ""}</td></tr>
<tr><th>Refill status</th><td class="{'bad' if st.blocked else ''}">{e(st.message)}</td></tr>
<tr><th>Allergy check{' (substitute)' if sub else ''}</th><td>{allergy_pill(rv.allergy.result)}<ul>{findings}</ul></td></tr>
</table></div>
<div class="card"><h2>Prescribed vs dispensed</h2>{timeline_mod.svg(rv.timeline, self.s.today)}
<ul>{gaps or '<li class="mute">No gaps.</li>'}</ul></div>
<div class="card"><h2>Dispense</h2>
<form class="grid" method="post" action="/rx/{e(rid)}/dispense">
<label>Action</label><select name="action">
<option value="full">Full (complete the current fill{': ' + e(remaining) if remaining else ''})</option>
<option value="partial">Partial</option><option value="substitute"{' selected' if sub else ''}>Substitute</option>
<option value="hold">Hold</option></select>
<label>Quantity ({e(st.unit)})</label><input name="quantity" type="number" step="any" min="0" placeholder="for partial or substitute">
<label>Substitute product</label><select name="substitute"><option value="">—</option>{options}</select>
<label>Reason</label><input name="reason" placeholder="needed for hold and substitute">
<label>Allergy override / review note</label><input name="allergy_note" placeholder="needed when the allergy check alerts">
<label>Pharmacist</label><input name="pharmacist" value="Demo Pharmacist">
<span></span><button>Record</button></form>
<p class="mute">To check a substitute for allergies first, pick it here: {''.join(
    f'<a href="/rx/{e(rid)}?sub={e(p.code)}">{e(p.code)}</a> ' for p in self.s.cat.products)}</p></div>
<div class="card wrap"><h2>History</h2><table><tr><th>When</th><th>Status</th><th>Type</th><th>Qty</th>
<th>Product</th><th>By</th></tr>{hist or '<tr><td colspan=6 class="mute">Nothing dispensed yet.</td></tr>'}</table></div>"""
        return page(med_text(rx), body, q.get("msg", ""), q.get("err", ""))

    # GET /patient/<id> -------------------------------------------------------
    def patient(self, pid: str, q) -> bytes:
        try:
            p = self.s.store.read("Patient", pid)
        except FhirError:
            p = None
        reqs, disp, stmts = self.s.patient_medication(pid)
        flags = self.s.reconcile(pid)
        allergies = self.s.store.search("AllergyIntolerance", patient=f"Patient/{pid}")

        def stmt_row(s):
            from .catalogue import dose_of
            d = dose_of(s)
            return (f"<tr><td>{e(med_text(s))}</td><td>{e(s.get('status'))}</td>"
                    f"<td>{e(f'{_fmt(d[0])} {d[1]}' if d else '—')}</td></tr>")

        flag_rows = []
        for f in flags:
            res = f.resolution
            if res:
                action = f'<span class="ok">Resolved ({e(res["outcome"])}) by {e(res["by"])}: {e(res["note"])}</span>'
            else:
                action = f"""<form method="post" action="/flag/{e(f.id)}/resolve" style="display:flex;gap:6px;flex-wrap:wrap">
<input type="hidden" name="patient" value="{e(pid)}"><select name="outcome" style="width:auto">
<option>corrected</option><option>confirmed intentional</option><option>false alert</option></select>
<input name="note" placeholder="evidence / note" style="flex:1;min-width:140px"><input type="hidden" name="actor" value="Demo Pharmacist">
<button>Resolve</button></form>"""
            flag_rows.append(f'<tr><td><b class="{"ok" if res else "bad"}">{e(f.label)}</b></td><td>{e(f.message)}'
                             f'<div class="mute">{e(", ".join(f.evidence))}</div></td><td>{action}</td></tr>')

        options = "".join(f'<option value="{e(x.code)}">{e(x.display)}</option>' for x in self.s.cat.products)
        body = f"""<h1>{e(patient_name(p))} · reconciliation</h1>
<div class="card"><h2>Allergies</h2>{''.join(f"<span class='pill bad'>{e(((a.get('code') or {}).get('text')) or '')}</span> " for a in allergies) or '<span class="mute">None recorded.</span>'}</div>
<div class="card wrap"><h2>Flags</h2><table><tr><th>Flag</th><th>Detail</th><th>Resolution</th></tr>
{''.join(flag_rows) or '<tr><td colspan=3 class="ok">No discrepancies.</td></tr>'}</table></div>
<div class="card wrap"><h2>Ordered</h2><table><tr><th>Medication</th><th>Status</th><th>Order</th></tr>
{''.join(f"<tr><td><a href='/rx/{e(r['id'])}'>{e(med_text(r))}</a></td><td>{e(r.get('status'))}</td><td>{e(r['id'])}</td></tr>" for r in reqs)}</table></div>
<div class="card wrap"><h2>Dispensed</h2><table><tr><th>When</th><th>Medication</th><th>Qty</th></tr>
{''.join(f"<tr><td>{e((d.get('whenHandedOver') or '')[:10])}</td><td>{e(med_text(d))}</td><td>{e(_fmt((d.get('quantity') or {}).get('value')))}</td></tr>" for d in disp if d.get('status') == 'completed') or '<tr><td colspan=3 class="mute">None.</td></tr>'}</table></div>
<div class="card wrap"><h2>Patient reports taking</h2><table><tr><th>Medication</th><th>Status</th><th>Dose</th></tr>
{''.join(stmt_row(s) for s in stmts) or '<tr><td colspan=3 class="mute">Nothing recorded.</td></tr>'}</table>
<h2 style="margin-top:14px">Record what the patient says</h2>
<form class="grid" method="post" action="/patient/{e(pid)}/statement">
<label>Product</label><select name="product"><option value="">— or type below —</option>{options}</select>
<label>Other (free text)</label><input name="text" placeholder="e.g. St John's wort">
<label>Dose per intake</label><div style="display:flex;gap:6px"><input name="dose" type="number" step="any">
<input name="unit" value="mg" style="max-width:90px"></div>
<label>Taking it?</label><select name="taking"><option value="yes">Yes</option><option value="no">No, not taking</option></select>
<input type="hidden" name="actor" value="Demo Pharmacist"><span></span><button>Add MedicationStatement</button></form></div>"""
        return page(patient_name(p), body, q.get("msg", ""), q.get("err", ""))

    def audit(self, q) -> bytes:
        rows = "".join(
            f"<tr><td>{e(x['at'][:19].replace('T', ' '))}</td><td>{e(x['actor'])}</td><td>{e(x['action'])}</td>"
            f"<td class='mute'>{e(json.dumps({k: v for k, v in x.items() if k not in ('at', 'actor', 'action') and v is not None}))}</td></tr>"
            for x in reversed(self.s.audit.entries()))
        return page("Audit trail", f"""<h1>Audit trail</h1><div class="card wrap"><table>
<tr><th>When (UTC)</th><th>Who</th><th>Action</th><th>Details</th></tr>{rows or '<tr><td colspan=4 class="mute">Empty.</td></tr>'}</table></div>""")

    def metrics(self, q) -> bytes:
        m = metrics_mod.compute(self.s)
        return page("Metrics", f"""<h1>Metrics on the synthetic demo set</h1>
<div class="card"><p class="mute">Compared with the known answers in demo.py. Only meaningful in demo mode.</p>
<pre class="wrap">{e(json.dumps(m, indent=2))}</pre></div>""")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, path: str, **params):
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
            self.send_response(303)
            self.send_header("Location", path + (f"?{qs}" if qs else ""))
            self.end_headers()

        def _route(self, method: str):
            url = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/") if p]
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            form = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
            try:
                if method == "GET":
                    if not parts:
                        return self._send(app.worklist(q))
                    if parts[0] == "rx" and len(parts) == 2:
                        return self._send(app.request(parts[1], q))
                    if parts[0] == "patient" and len(parts) == 2:
                        return self._send(app.patient(parts[1], q))
                    if parts == ["audit"]:
                        return self._send(app.audit(q))
                    if parts == ["metrics"]:
                        return self._send(app.metrics(q))
                if method == "POST":
                    if parts[:1] == ["rx"] and parts[2:] == ["dispense"]:
                        rid = parts[1]
                        qty = float(form["quantity"]) if form.get("quantity") else None
                        try:
                            md = app.s.dispense(rid, form.get("action", ""), form.get("pharmacist", ""), qty,
                                                form.get("substitute") or None, form.get("reason", ""),
                                                form.get("allergy_note", ""))
                        except DispenseRefused as ex:
                            return self._redirect(f"/rx/{rid}", err=str(ex))
                        return self._redirect(f"/rx/{rid}", msg=f"Recorded MedicationDispense/{md['id']} ({md['status']}).")
                    if parts[:1] == ["patient"] and parts[2:] == ["statement"]:
                        pid = parts[1]
                        try:
                            app.s.add_statement(pid, form.get("product"), form.get("text", ""),
                                                float(form["dose"]) if form.get("dose") else None,
                                                form.get("unit", "mg"), form.get("taking") != "no", form.get("actor", ""))
                        except DispenseRefused as ex:
                            return self._redirect(f"/patient/{pid}", err=str(ex))
                        return self._redirect(f"/patient/{pid}", msg="MedicationStatement recorded.")
                    if parts[:1] == ["flag"] and parts[2:] == ["resolve"]:
                        pid = form.get("patient", "")
                        try:
                            app.s.resolve_flag(parts[1], form.get("actor", ""), form.get("outcome", ""), form.get("note", ""))
                        except (DispenseRefused, KeyError) as ex:
                            return self._redirect(f"/patient/{pid}", err=str(ex))
                        return self._redirect(f"/patient/{pid}", msg="Flag resolved.")
                self._send(page("Not found", "<h1>Not found</h1>"), 404)
            except FhirError as ex:
                self._send(page("FHIR error", f"<h1>FHIR error</h1><pre>{e(str(ex))}</pre>"), 502)
            except Exception:
                self._send(page("Error", f"<h1>Error</h1><pre>{e(traceback.format_exc())}</pre>"), 500)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(service: PharmacyService, host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(App(service)))
    print(f"Pharmacy simulator on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
