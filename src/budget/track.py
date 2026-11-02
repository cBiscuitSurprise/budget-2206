import csv
import json
from datetime import date
from pathlib import Path

from budget.known import hits

ACTUALS_FIELDS = ["month", "debt_id", "balance", "as_of"]


def save_snapshot(plans: Path, snap: dict) -> Path:
    for k in ("committed", "start", "debts", "budget"):
        if k not in snap:
            raise ValueError(f"snapshot missing {k}")
    date.fromisoformat(snap["committed"])
    plans.mkdir(exist_ok=True)
    path = plans / f"{snap['committed']}.json"
    n = 2
    while path.exists():
        path = plans / f"{snap['committed']}-{n}.json"
        n += 1
    path.write_text(json.dumps(snap, indent=1) + "\n")
    return path


def load_snapshots(plans: Path) -> list[dict]:
    return [json.loads(f.read_text()) | {"file": f.name} for f in sorted(plans.glob("*.json"))]


def load_actuals(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    if path.exists():
        for r in csv.DictReader(path.open()):
            out.setdefault(r["month"], {})[r["debt_id"]] = float(r["balance"])
    return out


def upsert_actuals(path: Path, month: str, balances: dict[str, float], as_of: str) -> None:
    rows = list(csv.DictReader(path.open())) if path.exists() else []
    rows = [r for r in rows if not (r["month"] == month and r["debt_id"] in balances)]
    rows += [{"month": month, "debt_id": k, "balance": f"{v:.2f}", "as_of": as_of} for k, v in balances.items()]
    rows.sort(key=lambda r: (r["month"], r["debt_id"]))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, ACTUALS_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _idx(start: str, month: str) -> int:
    (y0, m0), (y1, m1) = (map(int, s.split("-")) for s in (start, month))
    return (y1 - y0) * 12 + m1 - m0


def planned(debt: dict, start: str, month: str) -> float:
    i = _idx(start, month)
    if i < 0:
        return debt["bal0"]
    return debt["bal"][i] if i < len(debt["bal"]) else 0.0


def compare(snap: dict, actuals: dict[str, dict[str, float]]) -> tuple[dict | None, list[dict]]:
    """Latest month's per-debt variance, and a total series over months with actuals (debts with an actual only)."""
    ids = {d["id"] for d in snap["debts"]}
    months = sorted(m for m, v in actuals.items() if ids & v.keys())
    series = []
    for m in months:
        ds = [d for d in snap["debts"] if d["id"] in actuals[m]]
        p, a = sum(planned(d, snap["start"], m) for d in ds), sum(actuals[m][d["id"]] for d in ds)
        series.append({"month": m, "planned": round(p, 2), "actual": round(a, 2), "variance": round(a - p, 2)})
    if not months:
        return None, series
    m = months[-1]
    rows = []
    for d in snap["debts"]:
        a = actuals[m].get(d["id"])
        p = planned(d, snap["start"], m)
        rows.append({"id": d["id"], "name": d["name"], "planned": round(p, 2), "actual": a, "done": d["done"],
                     "variance": None if a is None else round(a - p, 2)})
    have = [r for r in rows if r["actual"] is not None]
    total = {k: round(sum(r[k] for r in have), 2) for k in ("planned", "actual", "variance")}
    return {"month": m, "rows": rows, "total": total}, series


def payload(root: Path, owed: dict[str, float] | None = None) -> dict:
    snaps = load_snapshots(root / "plans")
    if not snaps:
        return {"plan": None, "compare": None, "series": [], "snapshots": []}
    cur = snaps[-1]
    cmp, series = compare(cur, load_actuals(root / "actuals.csv"))
    meta = ("committed", "note", "scenario", "strategy", "budget", "start", "months", "interest")
    return {"plan": {k: cur.get(k) for k in meta}, "compare": cmp, "series": series,
            "snapshots": [{k: s.get(k) for k in meta} for s in snaps]}


def seed(root: Path, snap: dict, nw: dict, month: str) -> dict:
    """Actual's Debt paydown budgets for `month` implied by a snapshot, against what budgets.yaml resolves to."""
    import yaml

    from budget.push import category_map
    from budget.sanity import month_budget

    i = _idx(snap["start"], month)
    if not 0 <= i < len(snap["debts"][0]["pay"]):
        raise ValueError(f"{month} is outside the plan ({snap['start']} onward)")
    cats = category_map(root / "categories.yaml")
    info = {d["id"]: d for d in [*nw["items"], *nw["debts"]]}
    irs = set(nw["plan"].get("irs", {}).get("debts", []))
    want: dict[str, float] = {}
    snowball = 0.0
    for d in snap["debts"]:
        pay, prior = d["pay"][i], d["bal0"] if i == 0 else d["bal"][i - 1]
        card = info.get(d["id"], {}).get("card")
        if card:
            floor = min(pay, max(35, prior * ((d["apr"] or 0) / 1200 + 0.01)))
            want["Card minimums"] = want.get("Card minimums", 0) + floor
            want["Snowball payment"] = want.get("Snowball payment", 0) + pay - floor
            snowball += pay - floor
            continue
        cat = "IRS" if d["id"] in irs else cats.get(info.get(d["id"], {}).get("category") or "", (0, None))[1]
        if cat:
            want[cat] = want.get(cat, 0) + pay
    dep = (snap.get("sav") or {}).get("dep") or []
    if i + 1 < len(dep) and dep[i + 1]:
        want["Snowball savings"] = dep[i + 1]
    base = yaml.safe_load((root / "budgets.yaml").read_text())
    known = {}
    for k in base.get("known") or []:
        if hits(k, month):
            known[k["category"]] = known.get(k["category"], 0) + k["amount"]
    cur = month_budget(root, month)
    out = {n: round(v + known.get(n, 0), 2) for n, v in want.items()}
    return {"month": month, "budgets": out, "current": {n: cur.get(n, 0) for n in out}, "snowball": round(snowball, 2)}
