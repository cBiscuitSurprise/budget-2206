import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_FIELD = re.compile(r"<([\w.]+)>([^<\r\n]*)")
_TXN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S)
_CARD = re.compile(r"<CCACCTTO>\s*<ACCTID>([^<\s]+)")


@dataclass(frozen=True)
class Transaction:
    account_id: str
    fitid: str
    posted: date
    amount: float
    name: str
    trntype: str
    checknum: str = ""
    card: str = ""
    memo: str = ""


@dataclass(frozen=True)
class Balance:
    account_id: str
    amount: float
    as_of: date


def _fields(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _FIELD.findall(block):
        if value.strip() and key not in out:
            out[key] = value.strip()
    return out


def _date(d: str) -> date:
    return date(int(d[:4]), int(d[4:6]), int(d[6:8]))


def _ident(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="latin-1")
    acct = re.search(r"<ACCTID>([^<\r\n]+)", text).group(1).strip()
    kind = re.search(r"<ACCTTYPE>([^<\r\n]+)", text)
    return acct, kind.group(1).strip() if kind else ""


def parse(path: Path, suffix: str = "") -> tuple[list[Transaction], Balance | None]:
    text = path.read_text(encoding="latin-1").replace("&amp;", "&")
    account_id = re.search(r"<ACCTID>([^<\r\n]+)", text).group(1).strip()
    if suffix:
        account_id += suffix
    txns = []
    for block in _TXN.findall(text):
        f = _fields(block)
        card = _CARD.search(block)
        txns.append(
            Transaction(
                account_id=account_id,
                fitid=f["FITID"],
                posted=_date(f["DTPOSTED"]),
                amount=float(f["TRNAMT"]),
                name=f.get("NAME", ""),
                trntype=f.get("TRNTYPE", ""),
                checknum=f.get("CHECKNUM", ""),
                card=card.group(1) if card else "",
                memo=f.get("MEMO", ""),
            )
        )
    bal = re.search(r"<LEDGERBAL>\s*<BALAMT>([^<\s]+)(?:</BALAMT>)?\s*<DTASOF>(\d{8})", text)
    return txns, Balance(account_id, float(bal.group(1)), _date(bal.group(2))) if bal else None


def load_dir(directory: Path) -> tuple[list[Transaction], dict[str, Balance]]:
    from budget.pdf import is_home_depot, parse_home_depot

    seen: dict[tuple[str, str], Transaction] = {}
    balances: dict[str, Balance] = {}
    paths = sorted(directory.rglob("*"))
    idents = {p: _ident(p) for p in paths if p.suffix.lower() in {".ofx", ".qfx", ".qbo"}}
    types: dict[str, set[str]] = {}
    for acct, kind in idents.values():
        types.setdefault(acct, set()).add(kind)
    for path in paths:
        suffix = path.suffix.lower()
        if path in idents:
            acct, kind = idents[path]
            shared = kind == "CREDITLINE" and len(types[acct]) > 1
            txns, bal = parse(path, ":creditline" if shared else "")
        elif suffix == ".pdf" and is_home_depot(path):
            txns, bal = parse_home_depot(path)
        else:
            continue
        for t in txns:
            seen.setdefault((t.account_id, t.fitid), t)
        if bal and (bal.account_id not in balances or bal.as_of >= balances[bal.account_id].as_of):
            balances[bal.account_id] = bal
    return sorted(seen.values(), key=lambda t: (t.posted, t.account_id, t.fitid)), balances
