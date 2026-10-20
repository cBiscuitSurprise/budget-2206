import csv
from datetime import date
from importlib.resources import files
from pathlib import Path

import click
import yaml

from budget.categorize import categorize, link_owner_money, load_annotations, load_rule_sets
from budget.ofx import load_dir
from budget.push import category_map
from budget.push import push as run_push
from budget.push import budgets_diff as run_diff
from budget.push import pull_into
from budget.report import build, entity_map, load_accounts
from budget.serve import serve as run_server
from budget.serve import serve_labeler

ROOT = Path.cwd()
ACCOUNTS = ROOT / "accounts.yaml"
ASSETS = ROOT / "assets.yaml"
CHECKS = ROOT / "checks"
CHECK_IMAGES = ROOT / "statements" / "checks"


def _load():
    txns, balances = load_dir(ROOT / "statements")
    entities = entity_map(load_accounts(ACCOUNTS))
    rows = categorize(txns, load_rule_sets(ROOT), load_annotations(ROOT / "annotations.csv"), entities)
    return link_owner_money(rows, entities), balances


def _build() -> str:
    return build(*_load(), ACCOUNTS, ASSETS)


@click.group()
def main() -> None:
    """Personal cash-flow tooling."""


@main.command()
@click.option("--out", type=click.Path(path_type=Path), default=ROOT / "reports" / "cashflow.html")
def report(out: Path) -> None:
    """Build the cash-flow report from statements/."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_build())
    click.echo(f"wrote {out}")


@main.command()
def sanity() -> None:
    """Independent checks on the statements and the Payoff tab's inputs."""
    from budget.sanity import run

    rows, balances = _load()
    checks = run(ROOT, rows, balances, build(rows, balances, ACCOUNTS, ASSETS), date.today())
    for ck in checks:
        click.secho(f"\n{'✓' if ck.ok else '✗'} {ck.name}", bold=True, fg="green" if ck.ok else "yellow")
        for line in ck.lines:
            click.echo(f"  {line}")
    click.echo(f"\n{sum(c.ok for c in checks)}/{len(checks)} checks clean")


@main.command()
@click.option("--month", default=lambda: date.today().strftime("%Y-%m"), show_default="this month")
def reconcile(month: str) -> None:
    """Itemize Actual's projected savings against the Payoff tab's available-for-debt."""
    from budget.sanity import payload, reconcile as rec

    rows, balances = _load()
    ck = rec(ROOT, payload(build(rows, balances, ACCOUNTS, ASSETS)), month)
    click.secho(ck.name, bold=True)
    for line in ck.lines:
        click.echo(line)


def _save_plan(snap: dict) -> str:
    from budget.track import save_snapshot

    return str(save_snapshot(ROOT / "plans", snap).relative_to(ROOT))


@main.command()
@click.option("--month", default=lambda: date.today().strftime("%Y-%m"), show_default="this month")
def track(month: str) -> None:
    """Record current plan-debt balances in actuals.csv, then show them against the committed plan."""
    from budget.sanity import payload as to_data
    from budget.track import compare, load_actuals, load_snapshots, upsert_actuals

    rows, balances = _load()
    nw = to_data(build(rows, balances, ACCOUNTS, ASSETS))["netWorth"]
    plan = nw["plan"]
    ids = [*plan["debts"], *plan.get("family", {})]
    owed = {d["id"]: d["owed"] for d in [*nw["items"], *nw["debts"]] if d["id"] in ids and d["owed"] is not None}
    upsert_actuals(ROOT / "actuals.csv", month, owed, nw["asOf"])
    click.echo(f"recorded {len(owed)} balances for {month} (as of {nw['asOf']}); missing: {', '.join(i for i in ids if i not in owed) or 'none'}")
    snaps = load_snapshots(ROOT / "plans")
    if not snaps:
        return click.echo("no committed plan yet: press Commit plan on the Payoff tab")
    cmp, _ = compare(snaps[-1], load_actuals(ROOT / "actuals.csv"))
    click.secho(f"\nplan {snaps[-1]['committed']} vs actual, {cmp['month']}", bold=True)
    for r in cmp["rows"]:
        a, v = ("-" if r["actual"] is None else f"{r['actual']:,.0f}"), ("-" if r["variance"] is None else f"{r['variance']:+,.0f}")
        click.echo(f"  {r['name'][:34]:34} plan {r['planned']:>10,.0f}  actual {a:>10}  var {v:>9}")
    t = cmp["total"]
    click.echo(f"  {'TOTAL (debts with an actual)':34} plan {t['planned']:>10,.0f}  actual {t['actual']:>10,.0f}  var {t['variance']:>+9,.0f}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
def serve(host: str, port: int) -> None:
    """Serve the report locally; rebuilds per request and auto-reloads when inputs change."""
    templates = Path(str(files("budget").joinpath("templates")))
    watch = [ROOT / "statements", *ROOT.glob("rules*.yaml"), ROOT / "annotations.csv", ACCOUNTS, ASSETS, templates]
    click.echo(f"serving http://{host}:{port}  (ctrl+c to stop)")
    run_server(_build, watch, host, port, _save_plan)


@main.group()
def checks() -> None:
    """Fetch check images and label checks."""


@checks.command()
@click.option("--url", default="https://www.usbank.com/", show_default=True)
def capture(url: str) -> None:
    """Open a logged-in-capable browser and save check images/API responses you view."""
    from budget.checks import capture as run_capture

    run_capture(ROOT / ".cache" / "usbank-profile", CHECKS / "_capture", url, click.echo)


@checks.command("fetch")
def fetch_images() -> None:
    """You log in to US Bank; then all check images not yet in checks/ are downloaded."""
    from budget.checks import fetch

    accts = [a for a, v in load_accounts(ACCOUNTS)["accounts"].items() if v["short"].startswith("USB")]
    fetch(ROOT / ".cache" / "usbank-profile", CHECK_IMAGES, accts, click.echo)


@checks.command()
@click.option("--port", default=8766, show_default=True)
def label(port: int) -> None:
    """Web app at 127.0.0.1 for labeling checks; writes annotations.csv."""
    from budget.checks import find_checks, load_suggestions, render, upsert_annotation

    def _render() -> str:
        cfg = load_accounts(ACCOUNTS)
        names = {k: v["name"] for k, v in cfg["entities"].items()}
        return render(find_checks(_load()[0], cfg), load_rule_sets(ROOT), names, CHECK_IMAGES, load_suggestions(CHECKS / "suggestions.csv"))

    def _save(body: dict) -> None:
        known = {c.txn.fitid for c in _load()[0]}
        if body.get("key") not in known:
            raise ValueError("unknown transaction")
        vals = {k: " ".join(str(body.get(k, "")).split()) for k in ("group", "category", "kind", "note")}
        upsert_annotation(ROOT / "annotations.csv", body["key"], **vals)

    watch = [ROOT / "annotations.csv", CHECKS, CHECK_IMAGES, *ROOT.glob("rules*.yaml"), Path(str(files("budget").joinpath("templates")))]
    click.echo(f"labeling at http://127.0.0.1:{port}  (ctrl+c to stop)")
    serve_labeler(_render, _save, CHECK_IMAGES, watch, "127.0.0.1", port)


@checks.command("apply")
@click.option("--dry-run", is_flag=True)
def apply_rules(dry_run: bool) -> None:
    """Label unlabeled checks whose OCR payee/memo matches checks/payees.yaml."""
    from budget.checks import auto_labels, find_checks, kind_for, load_ocr, load_payee_rules, upsert_annotation

    rule_sets = load_rule_sets(ROOT)
    hits = auto_labels(
        find_checks(_load()[0], load_accounts(ACCOUNTS)), load_ocr(CHECKS / "ocr.csv"),
        load_payee_rules(CHECKS / "payees.yaml"), rule_sets,
    )
    for c, rule, r in hits:
        note = " — ".join(v for v in (r["payee"], r["memo"]) if v)
        click.echo(f"{c.short} #{r['checknum']:>4} {-c.amount:>10.2f}  {rule.group} › {rule.category}  ({note})")
        if not dry_run:
            kind = kind_for(rule_sets, c.entity, rule.group, rule.category)
            upsert_annotation(ROOT / "annotations.csv", c.fitid, rule.group, rule.category, kind, note)
    click.echo(f"{'would label' if dry_run else 'labeled'} {len(hits)} checks")


@main.command()
@click.option("--url", default="http://localhost:5006", show_default=True)
@click.option("--password", envvar="ACTUAL_PASSWORD", required=True)
@click.option("--file", "file_", default="My Finances", show_default=True)
@click.option("--dry-run", is_flag=True)
def push(url: str, password: str, file_: str, dry_run: bool) -> None:
    """Push personal transactions since 2026-01-01 to Actual. Re-runs skip anything already imported."""
    rows, balances = _load()
    counts = run_push(rows, balances, load_accounts(ACCOUNTS), category_map(ROOT / "categories.yaml"), yaml.safe_load((ROOT / "cash.yaml").read_text()), yaml.safe_load(ASSETS.read_text()), url, password, file_, dry_run)
    click.echo(("dry run: " if dry_run else "") + ", ".join(f"{k} {v}" for k, v in counts.items()))


@main.group()
def budgets() -> None:
    """Sync category budgets between budgets.yaml and Actual."""


def _months(tokens: tuple[str, ...]) -> list[date]:
    out = []
    for tok in tokens:
        lo, _, hi = tok.partition("..")
        d, end = date.fromisoformat(f"{lo}-01"), date.fromisoformat(f"{hi or lo}-01")
        while d <= end:
            out.append(d)
            d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return out


def _sync(direction: str | None, months: tuple[str, ...], url: str, password: str, file_: str) -> None:
    path = ROOT / "budgets.yaml"
    plan = yaml.safe_load(path.read_text()) or {}
    cats = category_map(ROOT / "categories.yaml")
    for when in _months(months):
        diff, have = run_diff(plan, cats, when, url, password, file_, direction == "push")
        click.echo(f"== {when:%Y-%m}")
        for name, actual, local in diff:
            click.echo(f"{name:28} actual {actual or 0:>9}  plan {local or 0:>9}")
        if direction == "pull":
            pull_into(plan, when, have)
        click.echo(f"{len(diff)} differ" + ("" if direction is None else f"; {direction}ed"))
    if direction == "pull":
        path.write_text(yaml.safe_dump(plan, sort_keys=False, allow_unicode=True))


def _sync_options(f):
    for opt in (
        click.option("--file", "file_", default="My Finances", show_default=True),
        click.option("--password", envvar="ACTUAL_PASSWORD", required=True),
        click.option("--url", default="http://localhost:5006", show_default=True),
        click.argument("months", nargs=-1, required=True),
    ):
        f = opt(f)
    return f


@budgets.command("diff")
@_sync_options
def budgets_diff(months, url, password, file_) -> None:
    """Show where MONTHS (YYYY-MM or YYYY-MM..YYYY-MM) differ between Actual and budgets.yaml."""
    _sync(None, months, url, password, file_)


@budgets.command("push")
@_sync_options
def budgets_push(months, url, password, file_) -> None:
    """Write budgets.yaml amounts to MONTHS (YYYY-MM or YYYY-MM..YYYY-MM) in Actual."""
    _sync("push", months, url, password, file_)


@budgets.command("pull")
@_sync_options
def budgets_pull(months, url, password, file_) -> None:
    """Record MONTHS' Actual amounts in budgets.yaml as months[...] overrides."""
    _sync("pull", months, url, password, file_)


@main.group()
def plan() -> None:
    """Work with the committed payoff plan."""


@plan.command("budgets")
@click.option("--month", default=None, help="YYYY-MM inside the plan (default: its first month)")
def plan_budgets(month: str | None) -> None:
    """Print budgets.yaml months overrides that make Actual's Debt paydown match the committed plan."""
    from budget.sanity import payload as to_data
    from budget.track import load_snapshots, seed

    snaps = load_snapshots(ROOT / "plans")
    if not snaps:
        raise click.ClickException("no committed plan: press Commit plan on the Payoff tab")
    snap = snaps[-1]
    rows, balances = _load()
    nw = to_data(build(rows, balances, ACCOUNTS, ASSETS))["netWorth"]
    r = seed(ROOT, snap, nw, month or snap["start"])
    click.echo(f"# plan {snap['committed']}, {r['month']}: paste under months: in budgets.yaml\n  {r['month']}:")
    for n, v in r["budgets"].items():
        click.echo(f"    {n}: {v:.2f}  # now {r['current'][n]:,.2f}")
    click.echo(f"\n# Actual's projected savings should then read ~{r['card_principal']:,.0f} (card principal paid), if the rest of the month is budgeted to actuals")
    click.echo("# Not seeded: Uncategorized, cash and Venmo spending has no budget line (see `budget reconcile`)")


@main.command()
@click.option("--min-amount", default=500.0, show_default=True)
@click.option("--out", type=click.Path(path_type=Path), default=ROOT / "reports" / "to_identify.csv")
def unknowns(min_amount: float, out: Path) -> None:
    """Export unidentified transactions for annotation (copy rows into annotations.csv)."""
    flagged = ("unidentified", "Uncategorized", "?")
    rows = [c for c in _load()[0] if any(f in c.category for f in flagged) and abs(c.txn.amount) >= min_amount]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "group", "category", "kind", "note", "date", "amount", "payee", "current_category"])
        for c in sorted(rows, key=lambda c: c.txn.amount):
            t = c.txn
            key = f"check:{t.checknum}" if t.checknum else t.fitid
            w.writerow([key, "", "", "", "", t.posted.isoformat(), f"{t.amount:.2f}", t.name, c.category])
    click.echo(f"wrote {len(rows)} rows to {out}")
