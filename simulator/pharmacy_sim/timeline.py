"""Prescribed-vs-dispensed timeline with gaps flagged (proposal slide 9).

Reads the same RefillStatus as the blocking rule, so the counts under each fill
always match what the dispense step enforces.

Gap flags:
- no_dispense:     active order, nothing dispensed `grace_days` after it was written
- overdue_refill:  active order with refills left, and the last fill's supply
                   (dispenseRequest.expectedSupplyDuration) ran out more than
                   `grace_days` ago
- exhausted:       all fills used; further fills are blocked (information)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape

from .refills import RefillStatus, _fmt


def to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(value[:10])


def supply_days(request: dict) -> int | None:
    d = (request.get("dispenseRequest") or {}).get("expectedSupplyDuration") or {}
    if d.get("value") is None:
        return None
    value = float(d["value"])
    unit = (d.get("code") or d.get("unit") or "d").lower()
    factor = {"d": 1, "day": 1, "days": 1, "wk": 7, "week": 7, "weeks": 7, "mo": 30, "month": 30}.get(unit, 1)
    return int(value * factor)


@dataclass
class Gap:
    kind: str
    message: str
    since: date | None = None


@dataclass
class Timeline:
    request_id: str
    authored: date | None
    status: RefillStatus
    holds: list[dict] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)


def build(request: dict, dispenses: list[dict], status: RefillStatus, today: date,
          grace_days: int = 3) -> Timeline:
    authored = to_date(request.get("authoredOn"))
    tl = Timeline(request.get("id"), authored, status,
                  holds=[d for d in dispenses if d.get("status") == "on-hold"])
    active = request.get("status") == "active"

    if status.blocked:
        tl.gaps.append(Gap("exhausted", f"All {status.fills_allowed} fills used. Further fills are blocked."))
    elif active and not status.fills and authored and (today - authored).days > grace_days:
        tl.gaps.append(Gap("no_dispense",
                           f"Active order with no dispense for {(today - authored).days} days.", authored))
    elif active and status.fills:
        days = supply_days(request)
        last = status.fills[-1]
        last_date = to_date(last.completed_at or last.started_at)
        if days and last.complete and last_date:
            runs_out = last_date + timedelta(days=days)
            if (today - runs_out).days > grace_days:
                tl.gaps.append(Gap("overdue_refill",
                                   f"Supply from fill {last.number} ran out on {runs_out.isoformat()}; "
                                   f"no refill since ({(today - runs_out).days} days).", runs_out))
    return tl


def svg(tl: Timeline, today: date, width: int = 760) -> str:
    """A small inline SVG: prescribed bar on top, one marker per fill below."""
    st = tl.status
    pts = [tl.authored or today, today]
    for f in st.fills:
        for v in (f.started_at, f.completed_at):
            if to_date(v):
                pts.append(to_date(v))
    start, end = min(pts), max(pts) + timedelta(days=2)
    span = max((end - start).days, 1)
    left, right = 120, width - 30

    def x(d: date) -> float:
        return left + (right - left) * (d - start).days / span

    rows = []
    label = f"Prescribed: {_fmt(st.prescribed_quantity)} {escape(st.unit)} + {st.repeats_allowed} repeats"
    rows.append(f'<text x="8" y="34" class="lbl">Prescribed</text>')
    rows.append(f'<rect x="{x(tl.authored or start):.1f}" y="24" width="{right - x(tl.authored or start):.1f}" '
                f'height="14" rx="3" class="rx"/>')
    rows.append(f'<text x="{x(tl.authored or start):.1f}" y="18" class="small">{label}</text>')
    rows.append(f'<text x="8" y="84" class="lbl">Dispensed</text>')
    rows.append(f'<line x1="{left}" y1="78" x2="{right}" y2="78" class="axis"/>')

    for f in st.fills:
        d0 = to_date(f.started_at) or start
        d1 = to_date(f.completed_at) or d0
        cls = "fill" if f.complete else "partial"
        rows.append(f'<rect x="{x(d0) - 5:.1f}" y="70" width="{max(x(d1) - x(d0), 0) + 10:.1f}" height="16" '
                    f'rx="4" class="{cls}"/>')
        left_txt = f"{f.refills_left_after} left" if f.complete else f"partial {_fmt(f.quantity)}"
        rows.append(f'<text x="{x(d0) - 5:.1f}" y="104" class="small">Fill {f.number}</text>')
        rows.append(f'<text x="{x(d0) - 5:.1f}" y="118" class="small">{left_txt}</text>')

    for h in tl.holds:
        d = to_date(h.get("whenPrepared") or h.get("whenHandedOver")) or today
        rows.append(f'<text x="{x(d) - 4:.1f}" y="66" class="hold">H</text>')

    for g in tl.gaps:
        if g.since and g.kind != "exhausted":
            rows.append(f'<rect x="{x(g.since):.1f}" y="72" width="{max(x(today) - x(g.since), 4):.1f}" '
                        f'height="12" class="gap"/>')

    rows.append(f'<line x1="{x(today):.1f}" y1="10" x2="{x(today):.1f}" y2="124" class="today"/>')
    rows.append(f'<text x="{x(today) + 3:.1f}" y="134" class="small">today</text>')
    rows.append(f'<text x="{left}" y="134" class="small">{start.isoformat()}</text>')

    return (f'<svg viewBox="0 0 {width} 140" width="100%" role="img" '
            f'aria-label="Prescribed versus dispensed timeline">{"".join(rows)}</svg>')
