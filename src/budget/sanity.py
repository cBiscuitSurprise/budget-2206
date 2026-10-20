import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median

import yaml

from budget.categorize import Categorized
from budget.ofx import Balance
from budget.pdf import is_home_depot, parse_home_depot

INTEREST = {"Credit card interest & fees", "Reserve line interest & OD fees", "Card interest"}


def _is_interest(name: str) -> bool:
    return "INTEREST" in name.upper() or "FINANCE CHARGE" in name.upper()


@dataclass
class Check:
    name: str
    ok: bool = True
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def flag(self, msg: str) -> None:
        self.ok = False
        self.lines.append(f"✗ {msg}")

    def note(self, msg: str) -> None:
        self.lines.append(f"  {msg}")


def payload(html: str) -> dict:
    return json.JSONDecoder().raw_decode(html[html.index("const D=") + len("const D=") :])[0]


def _month_range(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    Y, M = map(int, b.split("-"))
    out = []
    while (y, m) <= (Y, M):
        out.append(f"{y}-{m:02d}")
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


class Ledger:
    """End-of-day balance per account, back-cast from the latest snapshot."""

    def __init__(self, rows: list[Categorized], balances: dict[str, Balance]):
        self.bal = balances
        self.txns: dict[str, list] = defaultdict(list)
        for c in rows:
            self.txns[c.txn.account_id].append(c.txn)

    def at(self, acct: str, d: date) -> float:
        b = self.bal[acct]
        return b.amount - sum(t.amount for t in self.txns[acct] if d < t.posted <= b.as_of) + sum(
            t.amount for t in self.txns[acct] if b.as_of < t.posted <= d
        )

    def adb(self, acct: str, end: date, days: int = 30) -> float:
        return sum(self.at(acct, end - timedelta(days=i)) for i in range(1, days + 1)) / days


def balance_chain(statements: Path, rows: list[Categorized], balances: dict[str, Balance], accounts: dict) -> Check:
    ck = Check("Statement balance chain")
    snaps = sorted((parse_home_depot(p)[1] for p in statements.rglob("*.pdf") if is_home_depot(p)), key=lambda b: b.as_of)
    by_acct = defaultdict(list)
    for c in rows:
        by_acct[c.txn.account_id].append(c.txn)
    for b0, b1 in zip(snaps, snaps[1:]):
        moved = sum(t.amount for t in by_acct[b1.account_id] if b0.as_of < t.posted <= b1.as_of)
        gap = (b1.amount - b0.amount) - moved
        msg = f"{accounts[b1.account_id]['short']} {b0.as_of}→{b1.as_of}: Δbalance {b1.amount - b0.amount:,.2f}, Σtxns {moved:,.2f}"
        ck.flag(f"{msg}, off {gap:,.2f}") if abs(gap) > 1 else None
    ck.note(f"{len(snaps) - 1} Home Depot statement pairs checked (OFX files carry only today's balance)")
    led = Ledger(rows, balances)
    for acct, v in accounts.items():
        if v["role"] != "card" or acct not in balances or not by_acct[acct]:
            continue
        days = sorted({t.posted for t in by_acct[acct]})
        hi = max(days, key=lambda d: led.at(acct, d))
        if led.at(acct, hi) > 50:
            ck.flag(f"{v['short']} back-cast balance goes to a credit of {led.at(acct, hi):,.2f} on {hi}: missing charges before then?")
        ck.note(f"{v['short']} back-cast opening {days[0]}: {led.at(acct, days[0] - timedelta(days=1)):,.2f}")
    return ck


def implied_apr(rows: list[Categorized], balances: dict[str, Balance], accounts: dict, assets: dict) -> Check:
    ck = Check("Implied APR (interest ÷ 30-day average balance)")
    rates = {d["account"]: d.get("rate") for d in (assets.get("debts") or {}).values() if d.get("account")}
    led = Ledger(rows, balances)
    for acct, v in accounts.items():
        if v["role"] != "card" or acct not in balances:
            continue
        charges = sorted(
            (c.txn for c in rows if c.txn.account_id == acct and c.category in INTEREST and c.txn.amount < 0 and _is_interest(c.txn.name)),
            key=lambda t: t.posted,
        )
        aprs = []
        for t in charges[-6:]:
            adb = -led.adb(acct, t.posted)
            if adb > 100:
                aprs.append(-t.amount / adb * 365 / 30 * 100)
        if not aprs:
            continue
        got, want = median(aprs), rates.get(acct)
        msg = f"{v['short']}: implied {got:.1f}% (last {len(aprs)}: {', '.join(f'{a:.0f}' for a in aprs)}) vs rate {want}"
        ck.flag(msg) if want is None or abs(got - want) > 3 else ck.note(f"✓ {msg}")
    return ck


def transfers(rows: list[Categorized], accounts: dict) -> Check:
    ck = Check("Transfers net to ~0")
    span = defaultdict(lambda: [date.max, date.min])
    for c in rows:
        s = span[c.txn.account_id]
        s[0], s[1] = min(s[0], c.txn.posted), max(s[1], c.txn.posted)
    by_cat = defaultdict(list)
    for c in rows:
        if c.kind == "transfer":
            by_cat[c.category].append(c.txn)
    for cat, ts in sorted(by_cat.items()):
        accts = {t.account_id for t in ts}
        if len(accts) == 1:
            ck.note(f"one-sided: {cat} ({accounts.get(next(iter(accts)), {}).get('short', '?')}, {sum(t.amount for t in ts):,.0f})")
            continue
        lo, hi = max(span[a][0] for a in accts), min(span[a][1] for a in accts)
        pool = sorted((t for t in ts if lo - timedelta(days=7) <= t.posted <= hi + timedelta(days=7)), key=lambda t: t.posted)
        used: set[int] = set()
        for i, t in enumerate(pool):
            if i in used:
                continue
            j = next(
                (j for j, u in enumerate(pool) if j != i and j not in used and abs(u.amount + t.amount) < 0.01 and abs((u.posted - t.posted).days) <= 7),
                None,
            )
            if j is not None:
                used |= {i, j}
        loose = [t for i, t in enumerate(pool) if i not in used and lo + timedelta(days=7) <= t.posted <= hi - timedelta(days=7)]
        msg = f"{cat} {lo}–{hi}: {len(used) // 2} matched pairs"
        if loose:
            ck.flag(f"{msg}, {len(loose)} unmatched: " + "; ".join(f"{accounts[t.account_id]['short']} {t.posted} {t.amount:,.2f}" for t in loose))
        else:
            ck.note(f"✓ {msg}")
    return ck


def bridge(D: dict) -> tuple[Check, float]:
    """Python port of the Payoff tab's ab() at default settings (no cuts)."""
    ck = Check("Available-for-debt bridge (baseline → model)")
    N, P = D["netWorth"], D["netWorth"]["plan"]
    by_id = {i["id"]: i for i in N["items"] + N["debts"]}
    in_plan = [by_id[i] for i in P["debts"] if i in by_id]
    BM = _month_range(P["baseline"]["from"], P["baseline"]["to"])
    ex_g, ex_c = set(P.get("exclude", {}).get("groups", [])), set(P.get("exclude", {}).get("categories", []))
    add_back = set(P.get("add_back", [])) | {d["category"] for d in in_plan if not d["card"] and d.get("category")}
    rows = [r for r in D["entities"]["personal"]["rows"] if r["m"] in BM]
    raw = sum(r["v"] for r in rows) / len(BM)
    cats: dict[str, dict] = {}
    for r in rows:
        if r["k"] == "transfer":
            continue
        o = cats.setdefault(r["c"], {"g": r["g"], "c": r["c"], "v": 0.0})
        o["v"] += r["v"]
    lines = []
    for o in cats.values():
        o["v"] /= len(BM)
        o["role"] = "excluded" if o["g"] in ex_g or o["c"] in ex_c else "replaced" if o["c"] in add_back else "kept"
        lines.append(o)
    tiny = [l for l in lines if abs(l["v"]) < 1]
    lines = [l for l in lines if abs(l["v"]) >= 1]
    by_cat = {l["c"]: l for l in lines}
    kept = [l for l in lines if l["role"] == "kept"]
    plan_lines = [{"name": a["name"], "cat": a.get("category"), "v": a["monthly"]} for a in P.get("adjustments", []) + P.get("income", [])]
    BL = P.get("budget") or []

    def total(ls):
        return sum(l["v"] for l in ls)

    transfers_net = sum(r["v"] for r in rows if r["k"] == "transfer") / len(BM)
    ck.note(f"{'Household net cash flow (incl. transfers), avg':<58}{raw:>12,.0f}")
    ck.note(f"{'− transfers between own accounts / card payments':<58}{-transfers_net:>12,.0f}")
    for role in ("excluded", "replaced"):
        ls = sorted((l for l in lines if l["role"] == role), key=lambda l: l["v"])
        ck.note(f"{'− ' + role + ' lines':<58}{-total(ls):>12,.0f}")
        for l in ls:
            ck.note(f"    {l['c'][:52]:<54}{-l['v']:>12,.0f}")
    ck.note(f"{'− sub-$1 lines dropped':<58}{-total(tiny):>12,.0f}")
    ck.note(f"{'= kept baseline':<58}{total(kept):>12,.0f}")
    avail = 0.0
    swaps = []
    per_cat: dict[str, tuple] = {}
    for b in BL:
        pl = [p for p in plan_lines if p["cat"] == b["n"]]
        src = [by_cat[c] for c in b.get("src") or [] if c in by_cat]
        ks = [l for l in src if l["role"] == "kept"]
        t = None if b.get("t") is None else (1 if b["g"] == "Income" else -1) * b["t"]
        if pl:
            base, frm = total(pl), "plan"
        elif t is not None and len(ks) == len(src):
            base, frm = t, "budget"
        else:
            base, frm = total(ks), "avg"
        if not (pl or ks or (not src and t)):
            continue
        avail += base
        per_cat[b["n"]] = (base, frm, "excluded" if src and all(l["role"] == "excluded" for l in src) else "replaced" if src and not ks else "")
        if frm != "avg":
            swaps.append((b["n"], frm, base - total(ks)))
    loose = [l for l in kept if not any(l["c"] in (b.get("src") or []) for b in BL)]
    loose_plan = [p for p in plan_lines if not any(b["n"] == p["cat"] for b in BL)]
    avail += total(loose) + total(loose_plan)
    for frm in ("plan", "budget"):
        ss = sorted((s for s in swaps if s[1] == frm), key=lambda s: s[2])
        ck.note(f"{'+ ' + frm + ' instead of baseline avg':<58}{sum(s[2] for s in ss):>12,.0f}")
        for n, _, dv in ss:
            if abs(dv) >= 1:
                ck.note(f"    {n[:52]:<54}{dv:>12,.0f}")
    ck.note(f"{'+ plan lines with no budget category':<58}{total(loose_plan):>12,.0f}")
    for p in loose_plan:
        ck.note(f"    {p['name'][:52]:<54}{p['v']:>12,.0f}")
    ck.note(f"{'= available for debt / mo':<58}{avail:>12,.0f}")
    check = total(kept) + sum(s[2] for s in swaps) + total(loose_plan)
    if abs(check - avail) > 1:
        ck.flag(f"bridge doesn't foot: {check:,.0f} vs {avail:,.0f}")
    if loose:
        ck.note(f"kept lines not in categories.yaml (counted at avg): {', '.join(l['c'] for l in loose)}")
    ck.data = {"per_cat": per_cat, "loose": total(loose), "loose_lines": [(l["c"], l["v"]) for l in loose], "loose_plan_lines": [(x["name"], x["v"]) for x in loose_plan], "loose_plan": total(loose_plan), "groups": {b["n"]: b["g"] for b in BL}}
    return ck, avail


def dropped_payments(D: dict) -> Check:
    ck = Check("Debt payments vs modeled debts")
    N, P = D["netWorth"], D["netWorth"]["plan"]
    by_id = {i["id"]: i for i in N["items"] + N["debts"]}
    in_plan = {i: by_id[i] for i in P["debts"] if i in by_id}
    for i in P["debts"]:
        if i not in by_id:
            ck.flag(f"plan.debts lists {i}, which isn't in assets.yaml")
    for i, d in in_plan.items():
        if not d.get("category"):
            continue
        modeled = d["owed"] is not None and d["owed"] > 0
        pay = (d.get("payment") or {}).get("monthly")
        if d["card"]:
            if not modeled and pay:
                ck.flag(f"{d['name']}: card with unknown balance; its {pay:,.0f}/mo payment stays in spending, the debt isn't modeled")
        elif not modeled:
            ck.flag(f"{d['name']}: payment category '{d['category']}' is removed from spending but the debt isn't modeled (owed={d['owed']}); {pay or 0:,.0f}/mo vanishes")
        elif not pay:
            ck.flag(f"{d['name']}: modeled with no payments seen in '{d['category']}' → minimum 0")
    plan_cats = {d.get("category") for d in in_plan.values()}
    sold = {p["name"] for i in N["items"] if i.get("sale") for p in i["sale"].get("pays", [])}
    for i, d in by_id.items():
        pay = (d.get("payment") or {}).get("monthly")
        if d.get("category") and d["category"] not in plan_cats and pay and d.get("kind") == "debt" and i not in (P.get("family") or {}) and d["name"] not in sold:
            ck.flag(f"{d['name']}: not in plan.debts; its {pay:,.0f}/mo payment stays in spending, the debt isn't modeled")
    if ck.ok:
        ck.note("✓ every replaced payment maps to a modeled debt")
    return ck


def stale(D: dict, today: date) -> Check:
    ck = Check("Stale or unknown balances")
    N, P = D["netWorth"], D["netWorth"]["plan"]
    want = set(P["debts"]) | set(P.get("family") or {})
    for d in N["items"] + N["debts"]:
        if d["id"] not in want and d.get("kind") == "debt":
            continue
        if d.get("kind") != "debt" and d["owed"] is None and not d.get("category"):
            continue
        tags = []
        if d["owed"] is None:
            tags.append("balance unknown")
        elif d.get("estimate"):
            tags.append("estimate")
        if d.get("asOf") and (today - date.fromisoformat(d["asOf"])).days > 30:
            tags.append(f"as of {d['asOf']}")
        if d.get("rate") is None:
            tags.append("APR assumed")
        if tags:
            owed = "?" if d["owed"] is None else f"{d['owed']:,.0f}"
            ck.flag(f"{d['name']} ({owed}): {', '.join(tags)}")
    return ck


def loans(D: dict, rows: list[Categorized], balances: dict[str, Balance], accounts: dict, assets: dict) -> Check:
    ck = Check("Loans: payment covers interest; statement interest matches rate")
    N, P = D["netWorth"], D["netWorth"]["plan"]
    apr = P.get("apr", {})
    irs = set((P.get("irs") or {}).get("debts", []))
    for d in N["items"] + N["debts"]:
        if d["id"] not in P["debts"] or d["card"] or not d["owed"] or d["id"] in irs:
            continue
        rate = d["rate"] if d["rate"] is not None else apr.get("vehicle" if d["kind"] == "vehicle" else "other", 0)
        pay = (d.get("payment") or {}).get("monthly") or (d.get("payment") or {}).get("last") or 0
        interest = d["owed"] * rate / 1200
        msg = f"{d['name']}: pays {pay:,.0f}/mo vs {interest:,.0f}/mo interest at {rate}%"
        if not pay:
            ck.flag(f"{d['name']}: no payment seen, so minimum 0; {interest:,.0f}/mo interest at {rate}% accrues until snowball reaches it")
        elif pay <= interest:
            ck.flag(f"{msg}: never pays off")
        else:
            ck.note(f"✓ {msg}" + (f" ({pay / interest:.1f}×)" if interest else ""))
    from budget.report import _loan_rows

    rates = {}
    for sect in ("properties", "vehicles"):
        for a in (assets.get(sect) or {}).values():
            loan = a.get("loan") or {}
            if loan.get("account"):
                rates[loan["account"]] = loan.get("rate")
    for d in (assets.get("debts") or {}).values():
        if d.get("account"):
            rates[d["account"]] = d.get("rate")
    for acct, v in accounts.items():
        if v["role"] != "loan" or acct not in balances:
            continue
        pays = [p for p in _loan_rows(rows, acct) if p["pay"] and p["interest"]]
        if not pays:
            ck.note(f"{v['short']}: no principal/interest split in memos")
            continue
        owed, implied = balances[acct].amount, []
        for p in reversed(pays):
            owed += p["principal"]
            implied.append((p["d"], p["interest"] / owed * 1200))
        implied.reverse()
        got, want = median(a for _, a in implied[-6:]), rates.get(acct)
        msg = f"{v['short']}: implied {got:.2f}% (last {min(6, len(implied))}: {', '.join(f'{a:.2f}' for _, a in implied[-6:])}) vs rate {want}"
        ck.flag(msg) if want is None or abs(got - want) > 0.5 else ck.note(f"✓ {msg}")
    return ck


def run(root: Path, rows: list[Categorized], balances: dict[str, Balance], html: str, today: date) -> list[Check]:
    accounts = yaml.safe_load((root / "accounts.yaml").read_text())["accounts"]
    assets = yaml.safe_load((root / "assets.yaml").read_text())
    D = payload(html)
    return [
        balance_chain(root / "statements", rows, balances, accounts),
        implied_apr(rows, balances, accounts, assets),
        transfers(rows, accounts),
        bridge(D)[0],
        dropped_payments(D),
        stale(D, today),
        loans(D, rows, balances, accounts, assets),
    ]


def month_budget(root: Path, month: str) -> dict[str, float]:
    """Budgets.yaml resolved for a month: default, then months[month], then known entries dated in it."""
    b = yaml.safe_load((root / "budgets.yaml").read_text())
    m = {**b["default"], **(b.get("months") or {}).get(month, {})}
    for k in b.get("known") or []:
        if str(k["date"])[:7] == month:
            m[k["category"]] = m.get(k["category"], 0) + k["amount"]
    return m


def reconcile(root: Path, D: dict, month: str) -> Check:
    ck = Check(f"Actual {month} projected savings → model available-for-debt")
    br, avail = bridge(D)
    pc = br.data["per_cat"]
    bud = month_budget(root, month)
    groups = br.data["groups"]
    rows = []
    for n, g in groups.items():
        a = (1 if g == "Income" else -1) * (bud.get(n) or 0)
        m, frm, why = pc.get(n, (0.0, "", "dropped"))
        if abs(a) < 0.5 and abs(m) < 0.5:
            continue
        note = {"excluded": "model excludes it", "replaced": "model hands it to the payoff", "dropped": "model drops it (excluded, or a payment the plan takes over)"}.get(why, "")
        if not note and frm == "plan":
            note = "model uses plan figure"
        elif not note and abs(a - m) >= 1:
            note = f"model uses {frm}"
        rows.append((n, a, m, note))
    actual = sum(r[1] for r in rows)
    ck.note(f"{'':<30}{'Actual':>10}{'Model':>10}{'Δ':>10}")
    for n, a, m, note in sorted(rows, key=lambda r: r[2] - r[1]):
        if abs(a - m) >= 1:
            ck.note(f"{n[:29]:<30}{a:>10,.0f}{m:>10,.0f}{m - a:>10,.0f}  {note}")
    loose = br.data["loose"] + br.data["loose_plan"]
    for n, v in [*br.data["loose_lines"], *br.data["loose_plan_lines"]]:
        ck.note(f"{('not budgeted: ' + n)[:29]:<30}{0:>10,.0f}{v:>10,.0f}{v:>10,.0f}  model counts Jun–Sep avg spend; Actual has no budget line")
    ck.note(f"{'TOTAL (projected savings)':<30}{actual:>10,.0f}{avail:>10,.0f}{avail - actual:>10,.0f}")
    return ck
