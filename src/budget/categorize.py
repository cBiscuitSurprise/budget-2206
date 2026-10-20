import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from budget.ofx import Transaction


@dataclass(frozen=True)
class Rule:
    group: str
    category: str
    kind: str
    patterns: tuple[re.Pattern, ...]
    trntype: str = ""
    accounts: tuple[str, ...] = ()
    since: date | None = None

    def matches(self, t: Transaction) -> bool:
        if self.trntype and t.trntype != self.trntype:
            return False
        if self.since and t.posted < self.since:
            return False
        if self.accounts and t.account_id not in self.accounts:
            return False
        return not self.patterns or any(p.search(t.name) or (t.memo and p.search(t.memo)) for p in self.patterns)


@dataclass(frozen=True)
class Categorized:
    txn: Transaction
    group: str
    category: str
    kind: str
    note: str = ""
    source: str = "rule"


def load_rules(path: Path) -> list[Rule]:
    rules = []
    for group, entries in yaml.safe_load(path.read_text()).items():
        for e in entries:
            rules.append(
                Rule(
                    group=group,
                    category=e["category"],
                    kind=e["kind"],
                    patterns=tuple(re.compile(str(m), re.I) for m in e.get("match", [])),
                    trntype=e.get("trntype", ""),
                    accounts=tuple(str(a) for a in e.get("accounts", [])),
                    since=e.get("since"),
                )
            )
    return rules


def load_rule_sets(root: Path) -> dict[str, list[Rule]]:
    """rules.yaml is the personal entity; rules.<entity>.yaml covers each other entity."""
    return {p.stem.partition(".")[2] or "personal": load_rules(p) for p in sorted(root.glob("rules*.yaml"))}


def load_annotations(path: Path) -> dict[str, dict[str, str]]:
    """Keyed by FITID or `check:<number>`; columns: key, group, category, kind, note."""
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["key"].strip(): row for row in csv.DictReader(f) if row.get("key", "").strip()}


def categorize(
    txns: list[Transaction],
    rule_sets: dict[str, list[Rule]],
    annotations: dict[str, dict[str, str]],
    entity_of: dict[str, str],
) -> list[Categorized]:
    out = []
    for t in txns:
        rules = rule_sets.get(entity_of.get(t.account_id, "personal"), [])
        note = annotations.get(t.fitid) or (annotations.get(f"check:{t.checknum}") if t.checknum else None)
        if note and note.get("category"):
            out.append(
                Categorized(
                    t,
                    note.get("group") or "Annotated",
                    note["category"],
                    note.get("kind") or ("income" if t.amount > 0 else "spend"),
                    note.get("note", ""),
                    "annotation",
                )
            )
            continue
        rule = next((r for r in rules if r.matches(t)), None)
        if rule:
            out.append(Categorized(t, rule.group, rule.category, rule.kind, note.get("note", "") if note else ""))
        elif t.amount > 0:
            out.append(Categorized(t, "Income", "Uncategorized income", "income", source="none"))
        else:
            out.append(Categorized(t, "Unknown", "Uncategorized", "spend", source="none"))
    return out


def link_owner_money(rows: list[Categorized], entity_of: dict[str, str], business: str = "sqez", days: int = 4) -> list[Categorized]:
    """Pair personal↔business moves of equal amount within `days`; the business side becomes a financing transfer."""
    ent = lambda c: entity_of.get(c.txn.account_id, "personal")
    open_ = {i: c for i, c in enumerate(rows) if ent(c) == business and "unidentified" in c.category.lower()}
    out = list(rows)
    for i, p in enumerate(rows):
        if ent(p) != "personal" or (p.group != "Crossover → SQeZ" and "unidentified" not in p.category.lower()):
            continue
        gap = {j: abs((b.txn.posted - p.txn.posted).days) for j, b in open_.items() if b.txn.amount == -p.txn.amount}
        j = min((j for j in gap if gap[j] <= days), key=gap.get, default=None)
        if j is None or p.category == "Purchases at SQeZ":
            continue
        b = open_.pop(j)
        into = p.txn.amount < 0
        link = f"matched {'personal' if into else business} {p.txn.posted if into else b.txn.posted}"
        out[j] = Categorized(
            b.txn, "Financing", "Owner contributions (from personal)" if into else "Owner draws (to personal)", "transfer",
            " — ".join(v for v in (b.note, link) if v), "linked",
        )
        out[i] = Categorized(
            p.txn, "Crossover → SQeZ", "Owner contributions to SQeZ" if into else "Owner draws from SQeZ",
            p.kind if into else "income", p.note, "linked",
        )
    return out
