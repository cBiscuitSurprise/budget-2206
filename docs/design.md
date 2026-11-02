# Budget: design

Personal finance, plus the books for the smoothie shop (SQeZ, Nourished Nest LLC), the rental (Home Base) and retirement. Self-hosted for a single user. Functional over resilient.

## Goals
- Load statements, then parse, categorize and route them automatically. A person only reviews a small queue.
- Weekly, monthly, quarterly and yearly reviews.
- Alerts when spending is off plan.
- **Pay down the debt** (added Oct 2026): keep a living payoff plan and check actual spending against it every month.

## Stack (Docker Compose on a personal server, reachable only over Tailscale)

| Layer | Tool | Status |
|---|---|---|
| Ingest + categorize | Python `uv` project, `click` CLI (`budget`) | **built** |
| Exploration reports | `budget report` builds `reports/cashflow.html` | **built** |
| Personal budget | Actual Budget | **next** |
| Business books | Bigcapital (fallback: Akaunting); check it is still maintained first | later |
| Retirement / investments | Ghostfolio | later |
| Aggregation | DuckDB, pulled nightly from each app's API | later |
| Alerts | ntfy + cron | later |
| Backups | restic, nightly, one per app volume | with Actual |

## Flow
```
bank QFX/OFX/PDF (downloaded by hand)  ─┐
                                        ├─► budget ingest + categorize ──► review queue (unknowns → rules)
check images (downloaded by hand)      ─┘          │
                                       ┌───────────┼──────────────┐
                                    Actual     Bigcapital     cashflow.html
                                  (personal)  (SQeZ, Home Base)  (exploration + payoff plan)
```

## Rules
- Each app owns its data. DuckDB is a read model that can be thrown away and rebuilt.
- **Ingest:** OFX/QFX first, CSV second, PDF only as a fallback (`pdf.py`). We download statements by hand.
- **No bank automation:** no automatic logins and no getting around bot detection.
  - This also applies to check images: download them by hand, then run `budget checks apply`.
- **Categorize:**
  - Exact payee rules first: `rules.yaml` and `rules.<entity>.yaml`. The first rule that matches wins, and `since:` handles card double-counting.
  - Then manual annotations in `annotations.csv`, keyed by transaction.
  - Anything left goes to `budget unknowns`. Each correction becomes a rule.
  - Claude helps with unknowns. It does not categorize automatically.
- Dedupe on the bank's transaction ID (FITID).
- **Business ↔ personal money moves only as owner contributions or draws:**
  - `link_owner_money` pairs moves of the same amount within 4 days.
  - Aggregation reconciles both sides so net worth doesn't count anything twice.
- **SQeZ financing is not revenue.** Loan proceeds count as a transfer.
  - SQeZ's real result ("all-in") is the SQeZ net, plus personal rows paying SQeZ costs, minus Square deposits that landed in personal accounts.
- Card charges that are business-related go to "Business: reimbursable" in Actual, then get exported to the business books.
- **Private data:**
  - Everything stays local or on the Tailnet.
  - Servers listen only on 127.0.0.1 or the Tailscale interface.
  - Nothing gets published to outside services.

## Source files

| File | Owns |
|---|---|
| `accounts.yaml` | Accounts, their role (anchor, savings, card, loan) and entity (personal, home_base at 50%, sqez) |
| `rules*.yaml` | Category rules for each entity |
| `annotations.csv` | One-off categories for single transactions; check labels |
| `assets.yaml` | Properties, vehicles, debts (balance, APR) and the payoff `plan` |
| `statements/` | Raw downloads. Never edited. |

## Debt payoff model (`reports/cashflow.html` → Payoff tab)

**Monthly budget for debt:**
- Start from personal spending, averaged over `plan.baseline` (Jun–Sep 2026).
- Remove one-offs (`exclude`) and the debt payments the plan takes over.
- Add `adjustments` (health insurance, SQeZ going dormant, setting aside tax on Parete pay) and the spouse's take-home pay.
- Add any cuts and extra income entered on the page.

**Simulation (`simulate()`):**
- Lives in `src/budget/templates/engine.js`, spliced into the report at build. Tests: `node --test tests/js`.
- **Snowball / mo** = minimum + cash left after all minimums in the first month the debt is the focus. Months with a shortfall, a sale lump or carried cash are skipped.
- Runs month by month.
- Interest accrues first, then minimums are paid, then everything left goes to the target debt.
- Card minimum: interest + 1% of the balance, at least $35. Loan minimum: the average payment over the last 12 months.
- If the budget is short of the minimums, the gap goes on a new card at `apr.card`. This makes a plan that can't work look like one.

**Start before the sale closes:**
- `plan.start` can precede `sale.close`. Months before the close subtract `plan.pre_sale` categories (the Valon mortgage) from the budget, and the sale proceeds land in the close month.

**Ordering:**
- Main debts are paid by snowball, avalanche or a manual order (`plan.order`). The page shows which order costs the least.
- The pod loan (`last`) comes after the main debts.
- Friends & family (`plan.family`, 0%, no minimums) come last, by priority.

**Scenarios and knobs:**
- Scenario tabs: Current plan, and Granny pod loan (`plan.pod`, funded from its start month).
- Knobs: strategy, APR assumptions, extra income, spending cuts (edited inline), and SQeZ asset sales (`plan.liquidate`).
  - Sales move only the what-if line. The baseline doesn't change.
- Knob state is saved in `localStorage`. It is not in the yaml.

**Taxes:**
- 2025 tax is on a payment plan. 2026 tax is an estimate ($45k; about $34k if the SQeZ loss counts against personal income).
- Both are modeled as IRS debts over `irs.months`, at the IRS rate.
- Neither is paid out of the Windjammer sale: closing nets about $0 after the SQeZ loan and costs.

### Findings (partly stale after the Oct 20 budget fix; as of 2026-10-19; numbers move as `assets.yaml` gets real figures)

| Scenario | Debt-free | Friends & family repaid |
|---|---|---|
| Current plan ($5,028/mo for debt, per `budget reconcile`; was $3,889) | re-read off the Payoff tab | re-read |
| + $150k granny pod loan Jan 2027 (8.5%, 20 yr, $1,302/mo) | never: short of minimums | never |
| Pod + $1,000/mo | Apr 2036 | Apr 2040 |

- **The pod needs about $900/mo more than the plan has.** September's grocery and dining level (~$1,300 vs a $3,450 average) would cover it, if it holds.
- **Biggest unknowns:**
  - real APRs (6 of 16 are assumed)
  - Synchrony and other card balances
  - the Valon payoff
  - how SQeZ files taxes
  - SQeZ's future (it loses about $10k/mo all-in)

### Committed plan and tracking
- **Commit plan** (Payoff tab) freezes the current scenario to `plans/<date>.json`: budget, order, APRs and each debt's month-by-month payment and balance. It needs `budget serve`; otherwise it downloads the file for you to move into `plans/`.
- Re-planning is a new commit. History stays; tracking compares against the latest.
- `budget track [--month]` records current balances of plan and family debts in `actuals.csv`, then prints plan vs actual. The **Plan vs actual** tab shows the same.
- `budget plan budgets [--month]` prints `budgets.yaml` overrides (loans, IRS, card interest) so Actual's Debt paydown matches the plan. Card principal paid is Actual's projected savings.
- `budget reconcile --month` itemizes Actual's projected savings against the Payoff tab's available-for-debt. Gaps: debt service Actual still budgets, spending with no budget line, and paycheck differences.

### Backlog
- Make the model easier to operate. Today the knobs live in a long page and the yaml.
- Move the plan targets into Actual's category budgets so tracking the plan is a monthly review, not a model run.
- Fix the horizontal overflow at phone width.

## Actual Budget setup (next)

**Scope:**
- **Personal entity only.** SQeZ and Home Base go to the business books later.
- Until then, the cashflow report stays the view for those entities.

**Deploy:**
- `actualbudget/actual-server` in Docker Compose, with a volume for `/data` that restic backs up.
- Reachable only over Tailscale.
- Pin the image version, because the import client below depends on it.

**Accounts:**

| Kind | Actual account | Notes |
|---|---|---|
| Checking, savings | on budget | US Bank checking and savings; Dogwood checking |
| Cards | on budget | Capital One, Dogwood card, reserve line, Home Depot, Synchrony |
| Kids' savings | off budget | Clara, Hazel, Ruby |
| Mortgages, vehicle loans, IRS, family loans | off budget | Balances follow `assets.yaml` (the model stays the source of truth) |
| SQeZ, Home Base accounts | not imported | Money moving to or from them is categorized as owner contributions or draws |

**Categories:**
- Rule *groups* map to Actual category groups, and rule categories map to Actual categories.
- `kind: transfer` between two on-budget accounts becomes an Actual transfer, not a category.

**Import client:**
- Recommended: `budget push` in Python, using `actualpy` with a pinned version that matches the server.
- Fallback: a small Node script on the official `@actual-app/api`. Use it if `actualpy` falls behind the server.
- Both import with `imported_id = FITID`, so running it again is a no-op.

**History:**
- Import from **2026-01-01**, so the 2026 tax year is complete.
- Opening balances come from each account's first statement on or after that date.
  - Dogwood statements start 2026-04-18, so those accounts open there.

**Budgets:**
- Seed the first month's category budgets from the payoff plan's kept spending, minus the cuts.
- Put the debt allotment in a "Debt paydown" category group.

**Bank sync:**
- SimpleFIN Bridge (~$15/yr) would replace the manual QFX downloads where it covers the bank.
- It is an approved aggregator, not scraping.
- Decide after a month of manual imports. Check coverage first for US Bank, Capital One, Chase, Mid America and the Citi (Home Depot) card.

## Import runbook

### Weekly (~15 min)
1. **Download** new QFX/OFX files from each bank's website into `statements/`. Overlapping date ranges are fine, because dedupe uses FITID. Mid America has its own folder.
2. **Categorize:** run `uv run budget unknowns`.
   - For each unknown, either add a rule to `rules*.yaml` (preferred) or annotate it in `annotations.csv`.
   - Repeat until the list is empty or holds only true one-offs.
3. **Checks:** download new check images by hand into `statements/checks/`, then run `uv run budget checks apply`. Use `budget checks label` for any it can't match.
4. **Push:** run `uv run budget push` (to be built). It prints what was added, skipped as duplicates, and changed.
5. **Reconcile in Actual:** each account's balance should match the statement's ledger balance. A mismatch usually means a missing file or a rule `since:` boundary.

### Monthly (first weekend)
1. Download the PDF-only statements: the Home Depot card and any loan statements.
2. Update `assets.yaml` balances for loans, the IRS and family debts, plus any new real APRs.
3. Run `uv run budget track`, then `uv run budget report`. Check the Plan vs actual tab: is the plan still on its debt-free date? If not, adjust the Payoff tab and Commit plan.
4. Actual budget review: compare planned vs actual by category. Carry the overspending into the cuts card.

### Quarterly / yearly
- **Quarterly:**
  - Estimated tax payments vs the $3,750/mo set-aside.
  - Re-baseline `plan.baseline` to the latest 4 months.
  - Change the rules if spending patterns shifted.
- **Yearly:**
  - Hand off to the CPA (SQeZ P&L, Home Base share, owner contributions and draws).
  - Close the IRS debt entries once they are paid.

## Build order
1. ~~Ingest/categorize CLI + exploration reports + payoff plan~~ (done, Oct 2026)
2. **Actual:**
   - deploy it
   - `budget push` with FITID dedupe
   - import from 2026-01-01
   - seed budgets from the plan
   - run the weekly runbook for a month
3. Decide on SimpleFIN.
4. Business books for SQeZ and Home Base.
5. Ghostfolio.
6. DuckDB aggregation and ntfy alerts (spend vs prorated budget, payoff plan slipping, tax reminders).

## Open questions
- **SQeZ's future:** keep running it, go dormant, or liquidate. This decides most of the plan.
- **How SQeZ files taxes:** a pass-through loss on the personal return or not. This is the $45k vs $34k 2026 estimate.
- **Missing figures:**
  - firm Valon payoff and closing costs
  - real APRs
  - Synchrony and other card balances
  - Camper 1's lender
  - the High Ridge value
- **SQeZ items to clear up:**
  - Check #158 ($25k "food trailer", 2025-05-30): it may be an owner draw.
  - Unmatched owner checks (~$2.3k).
  - 2024 deposits nobody has identified.
- **Business accounts:** separate business accounts and cards for SQeZ. Confirm with the CPA.
