import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from budget.ofx import Balance, Transaction

_CHECK = re.compile(r"^CHECK\s*#?\s*(\d+)", re.I)
MATCH_DAYS = 3
USER_AGENT = "budget-cli/0.1"
NOT_READY_RETRIES = 10
NOT_READY_WAIT = 30


class NotReady(Exception):
    pass


def _open(req: urllib.request.Request, timeout: int):
    req.add_header("User-Agent", USER_AGENT)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 307 and "not ready" in body.lower():
            raise NotReady from None
        raise RuntimeError(f"SimpleFIN {e.code} from {urlsplit(req.full_url).hostname}: {body}") from None


def claim(setup_token: str, dest: Path) -> None:
    claim_url = base64.b64decode(setup_token.strip()).decode()
    req = urllib.request.Request(claim_url, method="POST", data=b"")
    with _open(req, 30) as r:
        access_url = r.read().decode().strip()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(access_url)


def fetch(access_url: str, start: date, end: date) -> dict:
    parts = urlsplit(access_url)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    epoch = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    req = urllib.request.Request(f"{base}/accounts?start-date={epoch(start)}&end-date={epoch(end + timedelta(days=1))}")
    token = base64.b64encode(f"{parts.username}:{parts.password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    for attempt in range(NOT_READY_RETRIES + 1):
        try:
            with _open(req, 120) as r:
                return json.load(r)
        except NotReady:
            if attempt == NOT_READY_RETRIES:
                raise RuntimeError("SimpleFIN response still not ready after retries") from None
            time.sleep(NOT_READY_WAIT)
    raise AssertionError("unreachable")


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def parse(path: Path, ids: dict[str, str]) -> tuple[list[Transaction], list[Balance]]:
    """ids maps SimpleFIN account id -> accounts.yaml key; unmapped accounts are skipped."""
    data = json.loads(path.read_text())
    txns, balances = [], []
    for a in data.get("accounts", []):
        acct = ids.get(a["id"])
        if not acct:
            continue
        balances.append(Balance(acct, float(a["balance"]), _day(a["balance-date"])))
        for t in a.get("transactions", []):
            if t.get("pending"):
                continue
            desc = (t.get("description") or t.get("payee") or "").strip()
            check = _CHECK.match(desc)
            txns.append(
                Transaction(
                    account_id=acct,
                    fitid=f"sf:{t['id']}",
                    posted=_day(t["posted"]),
                    amount=float(t["amount"]),
                    name=desc,
                    trntype="CHECK" if check else ("CREDIT" if float(t["amount"]) > 0 else "DEBIT"),
                    checknum=check.group(1) if check else "",
                    memo=(t.get("memo") or "").strip(),
                )
            )
    return txns, balances


def merge(statement_txns: list[Transaction], feed_txns: list[Transaction]) -> list[Transaction]:
    """Feed rows that a downloaded statement already covers are dropped: same account and amount within MATCH_DAYS, on or before that account's last statement date."""
    last: dict[str, date] = {}
    pool: dict[tuple[str, float], list[date]] = defaultdict(list)
    for t in statement_txns:
        last[t.account_id] = max(last.get(t.account_id, t.posted), t.posted)
        pool[(t.account_id, round(t.amount, 2))].append(t.posted)
    kept = []
    for t in sorted(feed_txns, key=lambda t: t.posted):
        cutoff = last.get(t.account_id)
        if cutoff and t.posted <= cutoff + timedelta(days=MATCH_DAYS):
            dates = pool[(t.account_id, round(t.amount, 2))]
            hit = next((d for d in dates if abs((d - t.posted).days) <= MATCH_DAYS), None)
            if hit is not None:
                dates.remove(hit)
                continue
        kept.append(t)
    return kept
