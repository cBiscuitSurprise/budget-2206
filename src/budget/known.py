import re
from datetime import date
from pathlib import Path

import yaml


def hits(k: dict, ym: str) -> bool:
    """Whether a `known` entry lands in YYYY-MM; `every: N` repeats it every N months from its date."""
    y, m = (int(x) for x in str(k["date"])[:7].split("-"))
    ty, tm = (int(x) for x in ym.split("-"))
    d = (ty - y) * 12 + tm - m
    n = int(k.get("every") or 0)
    return d == 0 or (n > 0 and d > 0 and d % n == 0)


def default_at(plan: dict, ym: str) -> dict:
    """`default` with every `starts[YYYY-MM]` at or before YYYY-MM applied in order."""
    out = dict(plan.get("default") or {})
    for k, cats in sorted((plan.get("starts") or {}).items()):
        if str(k) <= ym:
            out.update(cats)
    return out


def _scalar(v: str) -> str:
    return v if re.fullmatch(r"[\w &'()/.-]+", v) else '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(path: Path, entries: list[dict]) -> int:
    text = path.read_text()
    head, sep, _ = text.partition("\nknown:")
    if not sep:
        raise ValueError("budgets.yaml has no known: block")
    lines = []
    for e in entries:
        date.fromisoformat(str(e["date"]))
        if not str(e["category"]).strip():
            raise ValueError("entry without category")
        parts = [f"date: {e['date']}", f"category: {_scalar(str(e['category']).strip())}", f"amount: {float(e['amount']):.2f}"]
        if str(e.get("note", "")).strip():
            parts.append(f"note: {_scalar(' '.join(str(e['note']).split()))}")
        if int(e.get("every") or 0) > 0:
            parts.append(f"every: {int(e['every'])}")
        if str(e.get("scale", "")).strip():
            parts.append(f"scales_with: {_scalar(str(e['scale']).strip())}")
        lines.append("  - {" + ", ".join(parts) + "}")
    new = head + "\nknown:\n" + "\n".join(lines) + "\n"
    yaml.safe_load(new)
    path.write_text(new)
    return len(lines)
