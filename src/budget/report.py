import json
import re
from calendar import monthrange
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path

import yaml

from budget.categorize import Categorized
from budget.ofx import Balance

_SPLIT = re.compile(r"(Principal|Interest|Escrow):\$([\d,]+\.\d\d)")


def _full_months(start: date, end: date) -> list[str]:
    y, m = start.year, start.month
    if start.day != 1:
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    months = []
    while (y, m) < (end.year, end.month) or (end.day == monthrange(end.year, end.month)[1] and (y, m) == (end.year, end.month)):
        months.append(f"{y}-{m:02d}")
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return months


def load_accounts(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def entity_map(cfg: dict) -> dict[str, str]:
    return {k: v.get("entity", "personal") for k, v in cfg["accounts"].items()}


def _row(c: Categorized, acct: dict, cardholders: dict, share: float) -> dict:
    t = c.txn
    return {
        "d": t.posted.isoformat(),
        "m": t.posted.strftime("%Y-%m"),
        "a": acct["short"],
        "r": acct["role"],
        "g": c.group,
        "c": c.category,
        "k": c.kind,
        "v": round(t.amount * share, 2),
        "n": t.name,
        "ck": t.checknum,
        "h": cardholders.get(t.card, ""),
        "id": t.fitid,
        "note": c.note,
    }


def _loan_rows(rows: list[Categorized], account_id: str) -> list[dict]:
    out = []
    for c in sorted((c for c in rows if c.txn.account_id == account_id), key=lambda c: c.txn.posted):
        t = c.txn
        split = {k.lower(): float(v.replace(",", "")) for k, v in _SPLIT.findall(t.memo)}
        payment = t.amount > 0
        out.append(
            {
                "d": t.posted.isoformat(),
                "m": t.posted.strftime("%Y-%m"),
                "n": t.memo.split(":")[0] if t.memo else t.name,
                "v": round(t.amount, 2),
                "pay": payment,
                "principal": split.get("principal", 0.0) if payment else 0.0,
                "interest": split.get("interest", 0.0) if payment else 0.0,
                "escrow": split.get("escrow", 0.0) if payment else 0.0,
            }
        )
    return out


def _payments(rows: list[Categorized], accounts: dict, category: str, share: float) -> dict | None:
    paid = sorted(
        (c for c in rows if c.category == category and c.txn.amount < 0 and accounts.get(c.txn.account_id, {}).get("role") != "loan"),
        key=lambda c: c.txn.posted,
    )
    if not paid:
        return None
    last = paid[-1].txn
    year = [c.txn for c in paid if (last.posted - c.txn.posted).days < 365]
    months = {t.posted.strftime("%Y-%m") for t in year}
    return {
        "last": round(-last.amount * share, 2),
        "lastDate": last.posted.isoformat(),
        "monthly": round(-sum(t.amount for t in year) * share / len(months), 2),
    }


def _net_worth(rows: list[Categorized], balances: dict[str, Balance], cfg: dict, assets_path: Path) -> dict:
    accounts, ents = cfg["accounts"], cfg["entities"]
    data = yaml.safe_load(assets_path.read_text()) if assets_path.exists() else {}
    used: set[str] = set()

    def share(entity: str) -> float:
        return ents[entity].get("share", 1.0)

    def liability(d: dict, entity: str) -> dict:
        sh, acct = share(entity), d.get("account")
        out = {
            "owed": None, "estimate": False, "asOf": None, "rate": d.get("rate"), "lender": d.get("lender"), "payment": None,
            "category": d.get("category"),
            "card": d.get("card", bool(acct) and accounts[acct]["role"] == "card"),
        }
        if acct:
            used.add(acct)
            b = balances.get(acct)
            if b:
                owed = -b.amount if accounts[acct]["role"] == "card" else b.amount
                out.update(owed=round(owed * sh, 2), asOf=b.as_of.isoformat())
            if accounts[acct]["role"] == "loan" and not d.get("category"):
                pays = [p for p in _loan_rows(rows, acct) if p["pay"]]
                if pays:
                    out["payment"] = {"last": round(pays[-1]["v"] * sh, 2), "lastDate": pays[-1]["d"], "monthly": None}
        elif d.get("balance") is not None:
            out.update(owed=round(d["balance"] * sh, 2), estimate=d.get("estimate", False), asOf=data.get("as_of") and str(data["as_of"]))
        if d.get("category"):
            out["payment"] = _payments(rows, accounts, d["category"], sh)
        return out

    items = []
    for kind, key in (("property", "properties"), ("vehicle", "vehicles")):
        for k, a in (data.get(key) or {}).items():
            e = a.get("entity", "personal")
            sh = share(e)
            value = a.get("value")
            sale = a.get("sale")
            item = {
                "id": k, "kind": kind, "entity": e, "share": sh,
                "name": a.get("address") or a.get("description") or k,
                "use": a.get("use"),
                "value": round(value * sh, 2) if value is not None else None,
                "valueSource": a.get("value_source"),
                **liability(a.get("loan") or {}, e),
            }
            if sale:
                item["sale"] = {
                    "price": round(sale["price"] * sh, 2),
                    "close": str(sale.get("close") or ""),
                    "commission": round(sale["price"] * sale.get("commission_pct", 0) * sh, 2),
                }
            items.append(item)

    debts = []
    for k, d in (data.get("debts") or {}).items():
        acct = d.get("account")
        e = accounts[acct].get("entity", "personal") if acct else d.get("entity", "personal")
        name = d.get("name") or (accounts[acct]["name"] if acct else k)
        debts.append({"id": k, "kind": "debt", "entity": e, "share": share(e), "name": name, **liability(d, e)})
    for k, v in accounts.items():
        if v["role"] in ("loan", "card") and k not in used:
            e = v.get("entity", "personal")
            debts.append({"id": k, "kind": "debt", "entity": e, "share": share(e), "name": v["name"], **liability({"account": k}, e)})

    cash = []
    for k, v in accounts.items():
        b = balances.get(k)
        if v["role"] in ("anchor", "savings", "checking") and not v.get("kids") and b:
            e = v.get("entity", "personal")
            cash.append({"name": v["name"], "entity": e, "value": round(b.amount * share(e), 2), "asOf": b.as_of.isoformat()})

    by_id = {d["id"]: d for d in debts}
    for item in items:
        if "sale" in item:
            pays = (data["properties"][item["id"]].get("sale") or {}).get("pays", [])
            item["sale"]["pays"] = [{"name": by_id[k]["name"], "owed": by_id[k]["owed"], "estimate": by_id[k]["estimate"]} for k in pays]

    plan = data.get("plan")
    if plan:
        plan["budget"] = _budget_lines(assets_path.parent)
    return {"asOf": str(data.get("as_of", "")), "items": items, "debts": debts, "cash": cash, "plan": plan}


def _budget_lines(root: Path) -> list[dict]:
    """Budget categories in categories.yaml order, with their rule-category sources and budgets.yaml default."""
    cats, plan = (yaml.safe_load((root / f).read_text()) for f in ("categories.yaml", "budgets.yaml"))
    default = plan.get("default") or {}
    return [{"g": g, "n": n, "src": srcs, "t": default.get(n)} for g, group in cats.items() for n, srcs in group.items()]


def build(rows: list[Categorized], balances: dict[str, Balance], accounts_path: Path, assets_path: Path) -> str:
    from budget.track import payload as tracking
    cfg = load_accounts(accounts_path)
    accounts, cardholders = cfg["accounts"], cfg.get("cardholders", {})
    unknown = {"short": "?", "role": "other", "entity": "personal"}

    entities = {}
    for eid, e in cfg["entities"].items():
        share = e.get("share", 1.0)
        ids = {k for k, v in accounts.items() if v.get("entity", "personal") == eid}
        anchor = next(k for k in ids if accounts[k]["role"] == "anchor")
        dates = [c.txn.posted for c in rows if c.txn.account_id == anchor]
        start, end = min(dates), max(dates)
        flow = [
            _row(c, accounts.get(c.txn.account_id, unknown), cardholders, share)
            for c in rows
            if accounts.get(c.txn.account_id, unknown).get("entity", "personal") == eid
            and accounts.get(c.txn.account_id, unknown)["role"] != "loan"
            and start <= c.txn.posted <= end
        ]
        entities[eid] = {
            "name": e["name"],
            "note": e.get("note", ""),
            "share": share,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fullMonths": _full_months(start, end),
            "accounts": {accounts[k]["short"]: accounts[k]["name"] for k in ids if accounts[k]["role"] != "loan"},
            "rows": flow,
            "allIn": [
                _row(c, accounts[c.txn.account_id], cardholders, 1.0)
                for c in rows
                if c.category in e.get("all_in", [])
                and accounts.get(c.txn.account_id, unknown).get("entity", "personal") == "personal"
                and start <= c.txn.posted <= end
            ],
        }

    debts = []
    for k, v in accounts.items():
        b = balances.get(k)
        e = cfg["entities"][v.get("entity", "personal")]
        debts.append(
            {
                "entity": v.get("entity", "personal"),
                "name": v["name"],
                "short": v["short"],
                "role": v["role"],
                "kids": v.get("kids", False),
                "share": e.get("share", 1.0),
                "balance": round(b.amount, 2) if b else None,
                "asOf": b.as_of.isoformat() if b else None,
                "loan": _loan_rows(rows, k) if v["role"] == "loan" else None,
            }
        )

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "entities": entities,
        "balances": debts,
        "netWorth": _net_worth(rows, balances, cfg, assets_path),
        "tracking": tracking(assets_path.parent),
    }
    template = files("budget").joinpath("templates/cashflow.html").read_text()
    return template.replace("/*__DATA__*/null", json.dumps(payload).replace("</", "<\\/"))
