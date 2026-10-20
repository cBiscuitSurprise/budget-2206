import csv
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from budget.categorize import Categorized, Rule

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".pdf")
_INTERESTING = re.compile(r"check|image|item|deposit", re.I)


@dataclass(frozen=True)
class Check:
    fitid: str
    account_id: str
    entity: str
    short: str
    posted: str
    amount: float
    checknum: str
    group: str
    category: str
    note: str
    labeled: bool


def find_checks(rows: list[Categorized], accounts: dict) -> list[Check]:
    out = []
    for c in rows:
        t = c.txn
        acct = accounts["accounts"].get(t.account_id, {})
        if acct.get("role") == "loan" or t.amount >= 0:
            continue
        if not (t.checknum or t.trntype == "CHECK" or "Check" in c.category):
            continue
        out.append(
            Check(
                fitid=t.fitid,
                account_id=t.account_id,
                entity=acct.get("entity", "personal"),
                short=acct.get("short", t.account_id),
                posted=t.posted.isoformat(),
                amount=t.amount,
                checknum=t.checknum,
                group=c.group,
                category=c.category,
                note=c.note,
                labeled=c.source == "annotation",
            )
        )
    return sorted(out, key=lambda k: (k.posted, k.checknum), reverse=True)


def image_for(images: Path, account_id: str, checknum: str) -> Path | None:
    if not checknum:
        return None
    return next((p for ext in IMAGE_EXT if (p := images / account_id / f"{checknum}{ext}").exists()), None)


def load_suggestions(path: Path) -> dict[str, dict[str, str]]:
    """Keyed by FITID; columns: fitid, payee, memo, group, category."""
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {r["fitid"]: r for r in csv.DictReader(f) if r.get("fitid")}


def load_ocr(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Keyed by (account, checknum); columns: account, checknum, date, payee, amount, memo, confidence."""
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {(r["account"], r["checknum"]): r for r in csv.DictReader(f)}


def ocr_for(c: Check, ocr: dict[tuple[str, str], dict[str, str]]) -> dict[str, str] | None:
    if c.checknum:
        return ocr.get((c.account_id, c.checknum))
    amt = f"{abs(c.amount):.2f}"
    hits = [r for (a, _), r in ocr.items() if a == c.account_id and r["amount"] == amt]
    return hits[0] if len(hits) == 1 else None


@dataclass(frozen=True)
class PayeeRule:
    group: str
    category: str
    payee: re.Pattern | None
    memo: re.Pattern | None

    def matches(self, r: dict[str, str]) -> bool:
        return all(p is None or p.search(r.get(f, "")) for p, f in ((self.payee, "payee"), (self.memo, "memo")))


def load_payee_rules(path: Path) -> dict[str, list[PayeeRule]]:
    import yaml

    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    pat = lambda s: re.compile(s, re.I) if s else None  # noqa: E731
    return {
        str(acct): [PayeeRule(r["group"], r["category"], pat(r.get("payee")), pat(r.get("memo"))) for r in rules]
        for acct, rules in raw.items()
    }


def auto_labels(
    checks: list[Check], ocr: dict, payee_rules: dict[str, list[PayeeRule]], rule_sets: dict[str, list[Rule]]
) -> list[tuple[Check, PayeeRule, dict[str, str]]]:
    """Unlabeled checks whose OCR text matches a payee rule."""
    out = []
    for c in checks:
        if c.labeled or not (r := ocr_for(c, ocr)):
            continue
        if rule := next((p for p in payee_rules.get(c.account_id, []) if p.matches(r)), None):
            out.append((c, rule, r))
    return out


def kind_for(rule_sets: dict[str, list[Rule]], entity: str, group: str, category: str) -> str:
    return next((r.kind for r in rule_sets.get(entity, []) if (r.group, r.category) == (group, category)), "spend")


def render(
    checks: list[Check], rule_sets: dict[str, list[Rule]], entity_names: dict[str, str], images: Path, suggestions: dict
) -> str:
    options = {
        e: sorted({(r.group, r.category, r.kind) for r in rules if r.kind == "spend"}) for e, rules in rule_sets.items()
    }
    payload = {
        "entities": entity_names,
        "options": {e: [{"g": g, "c": c, "k": k} for g, c, k in v] for e, v in options.items()},
        "checks": [
            {
                **c.__dict__,
                "image": (f"/img/{p.relative_to(images).as_posix()}" if (p := image_for(images, c.account_id, c.checknum)) else ""),
                "suggest": suggestions.get(c.fitid, {}),
            }
            for c in checks
        ],
    }
    template = files("budget").joinpath("templates/checks.html").read_text()
    return template.replace("/*__DATA__*/null", json.dumps(payload).replace("</", "<\\/"))


def upsert_annotation(path: Path, key: str, group: str, category: str, kind: str, note: str) -> None:
    fields = ["key", "group", "category", "kind", "note"]
    rows = []
    if path.exists():
        with path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("key", "").strip() != key]
    if category:
        rows.append({"key": key, "group": group, "category": category, "kind": kind, "note": note})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


GRAPHQL = "https://onlinebanking.usbank.com/digital/api/customer-management/graphql/v2"
_TXNS_Q = """query txnsDetails($input: TxnsAcctSearchRequestInput) { txnsDetails(txnsAcctSearchRequestInput: $input) {
 txnsResponse { postedTransactions { transactionId accountToken accountNumber transactionAmount postedDateTime
 checkTransactionDetails { checkNumber checkCaptureDate checkDocumentId } } } } }"""
_EDOC_Q = """query eDocuments($edocumentsInput: EdocumentsInput) { edocuments(edocumentsInput: $edocumentsInput) {
 documentGroups { documents { content format pageInfo } } errors { code message } } }"""
_SKIP_HEADERS = {"content-length", "cookie", "host", "accept-encoding", "connection"}


def _amount(v: float) -> str:
    v = abs(float(v))
    return str(int(v)) if v == int(v) else str(v)


def fetch(profile: Path, images: Path, account_ids: list[str], log: Callable[[str], None], page_size: int = 50) -> None:
    """You log in; then every listed check without a local image is downloaded as <acct>/<num>.png (+ -back.png)."""
    import base64

    from playwright.sync_api import Error, sync_playwright

    by_last4 = {a[-4:]: a for a in account_ids}
    headers: dict[str, str] = {}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False, no_viewport=True)

        def on_request(r) -> None:
            if r.url.startswith(GRAPHQL) and r.method == "POST":
                headers.clear()
                headers.update({k: v for k, v in r.all_headers().items() if not k.startswith(":") and k not in _SKIP_HEADERS})

        ctx.on("request", on_request)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://onlinebanking.usbank.com/")
        log("log in (captcha/SMS); fetching starts once the dashboard loads")
        for _ in range(900):
            if headers:
                break
            try:
                page.wait_for_timeout(1000)
            except Error:
                log("browser closed before login")
                return
        else:
            log("timed out waiting for login")
            return
        page.wait_for_timeout(3000)

        def gql(query: str, variables: dict) -> dict:
            r = ctx.request.post(GRAPHQL, headers=headers, data=json.dumps({"query": query, "variables": variables}))
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status}: {r.text()[:200]}")
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(json.dumps(body["errors"])[:300])
            return body["data"]

        found: dict[tuple[str, str], tuple[dict, dict]] = {}
        seen: set[str] = set()
        for n in range(1, 500):
            inp = {"accountTokens": [], "limit": str(page_size), "offset": str((n - 1) * page_size + 1),
                   "pageNumber": str(n), "pageName": "UNIVERSAL_ACTIVITY_CD"}
            txns = gql(_TXNS_Q, {"input": inp})["txnsDetails"]["txnsResponse"]["postedTransactions"] or []
            new = [t for t in txns if t["transactionId"] not in seen]
            if not new:
                break
            seen.update(t["transactionId"] for t in new)
            for t in new:
                ck = t.get("checkTransactionDetails") or {}
                acct = by_last4.get((t.get("accountNumber") or "")[-4:])
                if ck.get("checkNumber") and ck.get("checkDocumentId") and acct:
                    found[(acct, ck["checkNumber"])] = (t, ck)
            log(f"page {n}: {len(seen)} transactions, {len(found)} checks, oldest {new[-1]['postedDateTime']}")

        todo = [(k, v) for k, v in sorted(found.items()) if not image_for(images, *k)]
        log(f"{len(found)} checks listed, {len(todo)} need images")
        for (acct, num), (t, ck) in todo:
            req = {"documentRequests": [{"documentTypeCode": "CHECKS_ONUS", "searchCriteria": [
                {"searchOperand": "itemSequenceNumber", "searchOperandValues": ck["checkDocumentId"], "searchOperator": "Equal To"},
                {"searchOperand": "captureDate", "searchOperandValues": ck["checkCaptureDate"], "searchOperator": "Equal To"},
                {"searchOperand": "amount", "searchOperandValues": _amount(t["transactionAmount"]), "searchOperator": "Equal To"},
            ]}], "accountToken": t["accountToken"], "documentOutputFormat": "PNG", "maxItemsCount": 100}
            try:
                groups = gql(_EDOC_Q, {"edocumentsInput": req})["edocuments"]["documentGroups"] or []
            except RuntimeError as e:
                log(f"#{num}: {e}")
                continue
            docs = [d for g in groups for d in g["documents"] or []]
            (images / acct).mkdir(parents=True, exist_ok=True)
            for d in docs:
                suffix = "" if d.get("pageInfo") == "Front" else f"-{(d.get('pageInfo') or 'page').lower()}"
                (images / acct / f"{num}{suffix}.{(d.get('format') or 'png').lower()}").write_bytes(base64.b64decode(d["content"]))
            log(f"#{num} {acct}: {len(docs)} image(s)" if docs else f"#{num}: no images returned")
            page.wait_for_timeout(400)
        log("done; closing browser")
        ctx.close()


def capture(profile: Path, out: Path, start_url: str, log: Callable[[str], None]) -> None:
    """Headed browser; records traffic while you log in and open check images. Ends when the window closes."""
    from playwright.sync_api import Error, sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    index = out / "requests.jsonl"
    seq = len(list(out.glob("*.*")))

    with sync_playwright() as p, index.open("a") as idx:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False, no_viewport=True, accept_downloads=True)

        def on_response(r) -> None:
            nonlocal seq
            ctype = (r.headers.get("content-type") or "").split(";")[0]
            entry = {"t": time.time(), "method": r.request.method, "status": r.status, "type": ctype, "url": r.url}
            if "graphql" in r.url:
                entry["req"] = r.request.post_data
            blob = r.url.startswith("blob:")
            keep = blob or ctype == "application/pdf" or "graphql" in r.url
            keep = keep or (ctype.endswith("json") and bool(_INTERESTING.search(r.url)))
            if keep and r.status == 200:
                try:
                    body = r.body()
                except Error:
                    body = b""
                if body.startswith(b"%PDF"):
                    ctype = "application/pdf"
                if len(body) > (8000 if blob else 0):
                    seq += 1
                    ext = {"application/pdf": "pdf", "image/jpeg": "jpg"}.get(ctype, ctype.split("/")[-1].split("+")[0])
                    name = f"{seq:04d}.{ext}"
                    (out / name).write_bytes(body)
                    entry["saved"] = name
                    log(f"saved {name}  {ctype}  {len(body):,} B")
            idx.write(json.dumps(entry) + "\n")
            idx.flush()

        def on_download(d) -> None:
            nonlocal seq
            seq += 1
            name = f"{seq:04d}-{d.suggested_filename}"
            d.save_as(out / name)
            log(f"downloaded {name}")

        ctx.on("response", on_response)
        ctx.on("page", lambda pg: pg.on("download", on_download))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("download", on_download)
        page.goto(start_url)
        log("log in, then open a few check images; close the browser window when done")
        while True:
            try:
                if not ctx.pages:
                    break
                ctx.pages[0].wait_for_timeout(1000)
            except Error:
                break
        try:
            ctx.close()
        except Error:
            pass
