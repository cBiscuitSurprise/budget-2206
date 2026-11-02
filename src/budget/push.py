from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from actual import Actual
from actual.queries import (
    create_account,
    create_budget,
    create_splits,
    create_transaction,
    create_transfer,
    get_account,
    get_accounts,
    get_budgets,
    get_or_create_category,
    get_transactions,
)

from budget.categorize import Categorized
from budget.known import default_at, hits
from budget.ofx import Balance

SINCE = date(2026, 1, 1)
TRANSFER_DAYS = 4
CASH_CATEGORY = "Cash withdrawals"


@dataclass(frozen=True)
class Target:
    name: str
    off_budget: bool
    import_txns: bool
    sign: int


def targets(cfg: dict) -> dict[str, Target]:
    out = {}
    for acct_id, a in cfg["accounts"].items():
        if a.get("entity", "personal") != "personal" or a.get("actual") is False:
            continue
        loan = a.get("role") == "loan"
        out[acct_id] = Target(a["name"], loan or bool(a.get("kids")), not loan, -1 if loan else 1)
    return out


def category_map(path: Path) -> dict[str, tuple[str, str]]:
    cfg = yaml.safe_load(path.read_text())
    return {src: (group, name) for group, cats in cfg.items() for name, srcs in cats.items() for src in srcs}


def _date_opening(s, account: str) -> None:
    for tx in get_transactions(s, account=account):
        if tx.category and tx.category.name == "Starting Balances":
            tx.set_date(SINCE - timedelta(days=1))


def _dec(x: float) -> Decimal:
    return Decimal(str(round(x, 2)))


def _pair_transfers(rows: list[Categorized]) -> tuple[list[tuple[Categorized, Categorized]], list[Categorized]]:
    outs = sorted((r for r in rows if r.txn.amount < 0), key=lambda r: r.txn.posted)
    ins = [r for r in rows if r.txn.amount > 0]
    pairs, used = [], set()
    for o in outs:
        best = min(
            (
                (abs((i.txn.posted - o.txn.posted).days), k)
                for k, i in enumerate(ins)
                if k not in used and i.txn.account_id != o.txn.account_id and i.txn.amount == -o.txn.amount
            ),
            default=None,
        )
        if best and best[0] <= TRANSFER_DAYS:
            used.add(best[1])
            pairs.append((o, ins[best[1]]))
    paired = {id(r) for p in pairs for r in p}
    return pairs, [r for r in rows if id(r) not in paired]


def loan_accounts(assets: dict, tgt: dict[str, Target]) -> dict[str, tuple[str, Decimal | None]]:
    """Rule category -> (Actual account, opening balance to create it with; None when statements already supply it)."""
    out = {}
    items = [(p.get("entity", "personal"), p.get("loan")) for p in assets.get("properties", {}).values()]
    items += [("personal", v.get("loan")) for v in assets.get("vehicles", {}).values()]
    items += [(d.get("entity", "personal"), d) for d in assets.get("debts", {}).values()]
    for entity, loan in items:
        if entity != "personal" or not loan or not loan.get("category"):
            continue
        if loan.get("account") in tgt:
            out[loan["category"]] = (tgt[loan["account"]].name, None)
        elif loan.get("name") and loan.get("balance") is not None:
            out[loan["category"]] = (loan["name"], -_dec(loan["balance"]))
    return out


def _shares(cash: dict, month: str) -> dict[str, float]:
    shares = cash.get("months", {}).get(month) or cash["default"]
    if abs(sum(shares.values()) - 1) > 1e-9:
        raise ValueError(f"cash shares for {month} must sum to 1")
    return shares


def _drop(t) -> None:
    if t.transferred_id is not None and t.transfer is not None:
        t.transfer.delete()
    t.delete()


def _sync_cash(s, withdrawals: list[Categorized], tgt: dict[str, Target], cats: dict[str, tuple[str, str]], cash: dict, counts: Counter, dry_run: bool) -> None:
    group_of = {name: group for group, name in cats.values()}
    parents = {t.financial_id: t for t in get_transactions(s, is_parent=True) if t.financial_id}
    for r in withdrawals:
        fid, amt = r.txn.fitid, _dec(-r.txn.amount)
        shares = _shares(cash, r.txn.posted.isoformat()[:7])
        names = list(shares)
        parts = {n: _dec(float(amt) * shares[n]) for n in names[:-1]}
        parts[names[-1]] = amt - sum(parts.values())
        want = {n: v for n, v in parts.items() if v}
        cur = parents.get(fid)
        if cur and {(c.category.name, -c.get_amount()) for c in cur.splits if c.category} == set(want.items()):
            continue
        counts["cash splits"] += 1
        if dry_run:
            continue
        if cur:
            cur.delete()
        for t in get_transactions(s):
            if t.financial_id == fid:
                _drop(t)
        kids = [
            create_transaction(s, r.txn.posted, tgt[r.txn.account_id].name, payee=r.txn.name, notes=r.note or None, category=get_or_create_category(s, n, group_of[n]), amount=-v, cleared=True)
            for n, v in want.items()
        ]
        create_splits(s, kids, notes=r.note or "").financial_id = fid


def _sync_loans(s, payments: list[Categorized], tgt: dict[str, Target], cats: dict[str, tuple[str, str]], loans: dict[str, tuple[str, Decimal | None]], counts: Counter, dry_run: bool) -> None:
    by_id = {t.financial_id: t for t in get_transactions(s) if t.financial_id}
    for r in payments:
        cur = by_id.get(r.txn.fitid)
        if cur and cur.transferred_id is not None:
            continue
        counts["loan payments"] += 1
        if dry_run:
            continue
        if cur:
            _drop(cur)
        group, name = cats.get(r.category, (r.group, r.category))
        src, dst = create_transfer(s, r.txn.posted, tgt[r.txn.account_id].name, loans[r.category][0], _dec(-r.txn.amount), r.txn.name)
        src.financial_id = r.txn.fitid
        src.category_id = get_or_create_category(s, name, group).id
        s.add(src)
        s.add(dst)


def push(rows: list[Categorized], balances: dict[str, Balance], cfg: dict, cats: dict[str, tuple[str, str]], cash: dict, assets: dict, url: str, password: str, file: str, dry_run: bool) -> Counter:
    tgt = targets(cfg)
    loans = loan_accounts(assets, tgt)
    mine = [r for r in rows if r.txn.account_id in tgt and tgt[r.txn.account_id].import_txns and r.txn.posted >= SINCE]
    counts: Counter = Counter()
    with Actual(url, password=password, file=file) as a:
        s = a.session
        have = {acc.name for acc in get_accounts(s)}
        for acct_id, t in tgt.items():
            if t.name in have:
                continue
            bal = balances.get(acct_id)
            opening = 0.0
            if bal:
                opening = t.sign * bal.amount - sum(r.txn.amount for r in mine if r.txn.account_id == acct_id)
            if not dry_run:
                create_account(s, t.name, _dec(opening), t.off_budget)
                _date_opening(s, t.name)
            counts["accounts"] += 1

        for name, opening in {v for v in loans.values() if v[1] is not None}:
            if name not in have:
                counts["loan accounts"] += 1
                if not dry_run:
                    create_account(s, name, opening, True)
                    _date_opening(s, name)

        withdrawals = [r for r in mine if r.category == CASH_CATEGORY and r.txn.amount < 0]
        mine = [r for r in mine if r not in withdrawals]
        _sync_cash(s, withdrawals, tgt, cats, cash, counts, dry_run)
        payments = [r for r in mine if r.category in loans and r.kind != "transfer" and r.txn.amount < 0]
        mine = [r for r in mine if r not in payments]
        _sync_loans(s, payments, tgt, cats, loans, counts, dry_run)
        existing = {t.financial_id for t in get_transactions(s) if t.financial_id}
        new = [r for r in mine if r.txn.fitid not in existing]
        counts["duplicate"] = len(mine) - len(new)
        pairs, singles = _pair_transfers([r for r in new if r.kind == "transfer"])
        singles += [r for r in new if r.kind != "transfer"]

        if dry_run:
            counts["added"] = len(singles) + 2 * len(pairs)
            counts["transfers"] = len(pairs)
            return counts

        for o, i in pairs:
            src, dst = create_transfer(s, o.txn.posted, tgt[o.txn.account_id].name, tgt[i.txn.account_id].name, _dec(-o.txn.amount), o.txn.name)
            src.financial_id, dst.financial_id = o.txn.fitid, i.txn.fitid
            s.add(src)
            s.add(dst)
            counts["transfers"] += 1
            counts["added"] += 2
        for r in singles:
            cat = None if r.kind == "transfer" else get_or_create_category(s, *reversed(cats.get(r.category, (r.group, r.category))))
            create_transaction(
                s,
                r.txn.posted,
                tgt[r.txn.account_id].name,
                payee=r.txn.name,
                notes=r.note or None,
                category=cat,
                amount=_dec(r.txn.amount),
                imported_id=r.txn.fitid,
                cleared=True,
            )
            counts["added"] += 1
        a.commit()
    return counts


def _month_amounts(s, month: date) -> dict[str, Decimal]:
    return {b.category.name: b.get_amount() for b in get_budgets(s, month) if b.category and b.get_amount()}


def _known(plan: dict, month: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for k in plan.get("known") or []:
        if hits(k, month.isoformat()[:7]):
            out[k["category"]] = out.get(k["category"], Decimal(0)) + _dec(k["amount"])
    return out


def resolve(plan: dict, month: date) -> dict[str, Decimal]:
    ym = month.isoformat()[:7]
    base = {**default_at(plan, ym), **((plan.get("months") or {}).get(ym) or {})}
    out = {n: _dec(v) for n, v in base.items()}
    for n, v in _known(plan, month).items():
        out[n] = out.get(n, Decimal(0)) + v
    return out


def pull_into(plan: dict, month: date, have: dict[str, Decimal]) -> None:
    key, known = month.isoformat()[:7], _known(plan, month)
    default = default_at(plan, key)
    over = {}
    for n in sorted(have.keys() | default.keys() | known.keys()):
        base = have.get(n, Decimal(0)) - known.get(n, Decimal(0))
        if base != _dec(default.get(n, 0)):
            over[n] = float(base)
    months = plan.setdefault("months", {})
    months.pop(key, None)
    if over:
        months[key] = over


def budgets_diff(plan: dict, cats: dict[str, tuple[str, str]], month: date, url: str, password: str, file: str, push: bool) -> tuple[list[tuple[str, Decimal | None, Decimal | None]], dict[str, Decimal]]:
    """Rows of (category, actual, plan) that differ, plus Actual's amounts. push=True writes the resolved plan to Actual."""
    group_of = {name: group for group, name in cats.values()}
    want = resolve(plan, month)
    names = set(plan.get("default", {})) | {n for m in [*(plan.get("months") or {}).values(), *(plan.get("starts") or {}).values()] for n in m} | {k["category"] for k in plan.get("known") or []}
    for name in names:
        if name not in group_of:
            raise KeyError(f"unknown category {name!r}")
    with Actual(url, password=password, file=file) as a:
        have = _month_amounts(a.session, month)
        diff = [(n, have.get(n), want.get(n)) for n in sorted(have.keys() | want.keys()) if have.get(n, Decimal(0)) != want.get(n, Decimal(0))]
        if push:
            for n, g in group_of.items():
                get_or_create_category(a.session, n, g)
            for n, _, new in diff:
                create_budget(a.session, month, get_or_create_category(a.session, n, group_of.get(n, "")), new or Decimal(0))
            a.commit()
    return diff, have
