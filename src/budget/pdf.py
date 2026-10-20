import hashlib
from functools import lru_cache
import re
from datetime import date
from pathlib import Path

from pypdf import PdfReader

from budget.ofx import Balance, Transaction

_LINE = re.compile(r"^(\d\d)/(\d\d) (.+?) \$ ([\d,]+\.\d\d)(-?)$")
_REF = re.compile(r"\s+(?=\S*\d)[A-Z0-9]{8,}$")
_CARD = re.compile(r"^CARD ENDING (\d{4})")


@lru_cache(maxsize=256)
def _read(path: Path, mtime_ns: int) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


def _text(path: Path) -> str:
    return _read(path, path.stat().st_mtime_ns)


def is_home_depot(path: Path) -> bool:
    return "HOME DEPOT CREDIT SERVICES" in _text(path)


def parse_home_depot(path: Path) -> tuple[list[Transaction], Balance]:
    text = _text(path)
    account_id = re.search(r"Account Number:? xxxx xxxx xxxx (\d{4})", text).group(1)
    mm, dd, yy = re.search(r"Closing Date (\d\d)/(\d\d)/(\d\d)", text).groups()
    closing = date(2000 + int(yy), int(mm), int(dd))
    balance = float(re.search(r"New Balance \$([\d,]+\.\d\d)", text).group(1).replace(",", ""))
    txns, card, seen = [], "", {}
    for line in text.splitlines():
        line = line.strip()
        if m := _CARD.match(line):
            card = m.group(1)
            continue
        m = _LINE.match(line)
        if not m:
            continue
        mo, dy, desc, amt, credit = m.groups()
        year = closing.year - (int(mo) > closing.month)
        posted = date(year, int(mo), int(dy))
        amount = float(amt.replace(",", "")) * (1 if credit else -1)
        name = _REF.sub("", desc).strip()
        payment = "PAYMENT" in name.upper() or "FINANCE CHARGE" in name.upper()
        key = f"{posted}|{desc}|{amount}"
        seen[key] = seen.get(key, 0) + 1
        fitid = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:20]
        txns.append(Transaction(account_id, fitid, posted, amount, name, "CREDIT" if credit else "DEBIT", card="" if payment else card))
    return txns, Balance(account_id, -balance, closing)
