# YNAB Toolkit

A configurable command-line companion for YNAB: categorize transactions, plan category funding from current or expected income, check card payment coverage, and inspect spending.

**All budget-changing CLI commands preview by default. Use `--apply` to write to YNAB.** Categorization also defaults to preview; this changes the previous automatic-approval behavior.

## Install and configure

Requires Python 3.13+, PDM, and a YNAB Personal Access Token. Categorization additionally uses an installed, authenticated Claude CLI; budgeting and reports do not need Claude. Playwright Chromium is needed only for optional Amazon enrichment.

```bash
git clone https://github.com/colinobriencork/ynab-toolkit.git
cd ynab-toolkit
pdm install
cp .env.example .env
cp config.example.toml config.toml
pdm run setup
python -m ynab_categorizer --help
```

Run commands with `pdm run ...` or `pdm run python -m ynab_categorizer ...` so they use the project environment. The direct Python examples below assume that environment is active.

Set `YNAB_API_TOKEN` in `.env` or the process environment. Supported process environment values override `.env`, which overrides top-level TOML settings. `config.toml` is optional; `--config /path/to/settings.toml` selects another file. An explicitly selected missing file is an error.

Choose a budget with `--budget "Your Budget Name"` or its ID. Otherwise, set `default_budget` in TOML or use the interactive selector. No personal budget names, IDs, income, or merchant rules are required in source code.

The TOML file supports `[defaults]` and `[budgets."Name or ID"]` overrides. Per-budget dictionaries merge with defaults; arrays replace defaults. If both ID and name entries exist, the name entry takes precedence. Unknown options are rejected to catch misspellings.

```toml
# Invented toy amounts, not a realistic household budget.
# All configured money amounts are integer YNAB milliunits: 1000 = 1 currency unit.
default_budget = "Household"

[defaults]
priority_groups = ["Essentials", "Savings", "Annual expenses", "Lifestyle"]
discretionary_group = "Lifestyle"
wants_mode = "refill"
model = "haiku"
guess_model = "opus"
timeout = 60
guess_timeout = 180

[budgets."Household"]
monthly_income = 120000
warn_ratio = 0.8
near_limit_ratio = 0.9
# Explicit dated income replaces automatic biweekly detection. Empty [] means none.
income_schedule = [{date = "2030-01-15", amount = 60000}, {date = "2030-01-30", amount = 60000}]
wants_overrides = { "Dining out" = 9000, "Hobbies" = 7000 }
merchant_rules = { "Known Clothing Shop" = "Essentials: Clothing" }
group_roles = { Savings = "saving", "Loan payments" = "repayment" }

[savings_pairs]
Retirement = ["retirement", "pension"]
```

Use your actual category/group names. Priority matching accepts a parenthetical suffix, such as `Savings (Reserved)`. Including an annual-expense group funds its targets; the default groups are Bills, Needs, Savings, Wants. Default Infrequent targets are excluded, but overspending is covered regardless of group. Income schedules are explicit dates, not repeating schedules: maintain future dates as months change.

## Categorization

```bash
pdm run categorize --budget "Household"
pdm run categorize --budget "Household" --apply
pdm run categorize --report reports/categorize-preview.json
```

Exact, case-insensitive merchant rules run first. Otherwise, a first-pass model uses transaction details and approved merchant history. Uncertain cases go to a second model with nearby transaction context and web tools. A transaction cannot serve as its own historical evidence. Group-qualified names and category IDs are supported; ambiguous names are rejected.

Transfers, splits, bare person-to-person transfers without a configured rule, and unresolved transactions are left for review. Confirmed model/rule decisions are approved when applied. Individual failures are recorded and the run continues; failed or unverifiable runs return a nonzero exit code. No interactive classification questions interrupt the run.

The first model has no built-in tools; the second is limited to WebSearch/WebFetch. Budget and merchant details are sent to Claude, and research may send merchant information to web services. YNAB, Amazon, and email credentials are removed from the subprocess environment. Tool restrictions are not an OS sandbox; your installed Claude configuration remains relevant.

### Optional Amazon enrichment

```bash
pdm run playwright install chromium
pdm run amazon-login
```

Configure `AMAZON_USERNAME`, `AMAZON_PASSWORD`, `AMAZON_OTP_SECRET_KEY`, `AMAZON_DOMAIN`, and **`AMAZON_BUDGET_NAME`** in `.env`. Enrichment requires an explicit matching budget name so one person's orders are not used for another budget. `AMAZON_OTP_SECRET_KEY` is the authenticator's base32 secret, not a six-digit code.

The browser profile is stored at `~/.config/ynab-categorizer/amazon-profile`. Expired sessions can open a login browser; a CAPTCHA or passkey challenge may require interaction. Parsing currently assumes English order dates and dollar-formatted Amazon totals; other marketplace formats are not supported automatically.

## Budget assignment and verification

```bash
pdm run assign --budget "Household"
pdm run phantom-assign --budget "Household"
pdm run phantom-assign --budget "Household" --apply
pdm run budget-check --budget "Household"
```

`assign` spends only positive Ready to Assign. It funds carried-over card debt, current overspending, then targets in configured priority order. Nonpositive RTA is reported as unavailable cash, not as proof that all categories are funded.

`phantom-assign` uses RTA plus expected income to lay out the month. It funds carried-over card debt, current overspending, and committed targets in full, then distributes the remainder across the discretionary group. **Commitments can exceed expected income:** the report identifies that uncovered amount. Negative RTA is expected when the plan depends on future income; category balances are not proof that the cash has already arrived.

Without an explicit income schedule, the planner detects recent biweekly payroll. It rejects stale patterns after a missed expected payday and reduces the influence of an unusually large latest payment. This is a heuristic, not a guarantee; use explicit dated income for monthly, irregular, or changing pay.

Discretionary allowances use recent average spending unless overridden. `wants_mode = "refill"` counts carryover funding; `"accumulate"` adds this month's allowance independently of carryover. Ordinary targets skip hidden categories, while negative hidden spending balances are included in overspending coverage and checks. Proportional allocations respect the budget's currency decimal precision (optional `currency_decimal_digits` override, 0–3).

Both commands share an application workflow:

1. Fetch a budget snapshot and calculate additions.
2. Check that the snapshot and affected assignments have not changed before writing.
3. Apply assignments, journaling each operation.
4. Fetch fresh card balances after YNAB moves money from spending categories.
5. Transfer surplus payment funding to underfunded cards, releasing the donor assignment first.
6. Read back actual assignments, RTA, remaining overspending, and card differences.

Card transfers do not send bank payments. A preview lists current-state possible transfers; the exact transfers are recalculated after funding. Changes are separate API requests, not an atomic transaction. A concurrent edit or failure stops further budgeting writes and returns a nonzero exit status. Run `budget-check` before retrying.

`budget-check` is read-only and reports RTA, expected income coverage, underfunded targets, overspending, and each card's surplus/shortfall. It returns nonzero for uncovered forecasts, overspending, or underfunded cards. Targets alone may remain underfunded intentionally. Use `--report PATH` for JSON.

## Correct historical categorization

```bash
pdm run correct --budget "Household" --transaction TRANSACTION_ID --category "Essentials: Clothing"
pdm run correct --budget "Household" --transaction TRANSACTION_ID --category "Essentials: Clothing" --apply
```

For a single unsplit expense, this moves the transaction and the matching assignment from the original category to the destination in the transaction's month. It preserves approval state, checks available balances and RTA, then reconciles current card funding displaced by historical edits. Refunds, transfers, and splits require manual handling. The command refuses stale transaction data or ambiguous categories.

## Run journals and recovery

Applied runs automatically write private JSON journals to `$XDG_STATE_HOME/ynab-categorizer/runs` (default `~/.local/state/ynab-categorizer/runs`). `--report PATH` chooses a different location or saves a preview. Journals record identifiers, previous/proposed values, completed writes, and failures. They contain private financial information; they are not source files.

```bash
pdm run restore /path/to/run.json
pdm run restore /path/to/run.json --apply
```

Restore reverses completed operations only when current values still match what the run wrote. It refuses to overwrite later edits. Uncertain writes—such as a lost response after a request—require inspection before restoration. Keep journals intact. Restoration also consists of separate API calls and is itself journaled.

## Spending reports and email

```bash
pdm run spending-report --budget "Household" --start 2026-07-01 --end 2026-09-30 --report reports/spending.json
pdm run spend-watch --budget "Household"
pdm run spend-watch --send
```

Spending reports show category outflows, inflows, and net spending; income; and configured saving/repayment roles. Uncategorised account transfers are excluded, while categorized transfers to tracking accounts follow their category role. Split transactions are counted by their parts. Reports include hidden historical categories and flag unapproved/unknown categories and merchants using multiple categories. Flags are prompts to review, not proof of an error. Refunds and reimbursements are shown together as category inflows rather than guessed from payee names.

`spend-watch` compares spending with each configured budget's `monthly_income` and thresholds. Optional `savings_pairs` maps comparison labels to category-name keywords. No savings comparisons run unless you configure this table; an empty table also disables them. Existing `MONTHLY_INCOME_<FIRST_WORD>` environment values remain supported in currency units; explicit per-budget `monthly_income` avoids first-word collisions and uses milliunits.

Email uses Gmail SMTP. Set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and optional `SPEND_WATCH_RECIPIENTS`; email is sent only with `--send`. Reports reflect YNAB's recorded balances and categories, not independently reconciled bank statements. The CLI currently uses English labels and dollar-style displays; amounts belong to the selected budget's currency and are never converted.

## Development and publishing

```bash
pdm run test
pdm run export-public /tmp/ynab-public
pdm run prepare-release /tmp/ynab-release-0.1.1
```

Tests use fake clients, HTTP mocks, and simulated failures; no live budgets, emails, model requests, or bank payments are needed. GitHub Actions runs the test suite on Python 3.13 and 3.14.

`export-public` creates a new source-only directory using an explicit file allowlist: application, tests, public examples, build files, and CI. It omits `.env`, `config.toml`, browser profiles, run journals, personal reports, local Claude settings, standalone personal analysis scripts, and `.git` history. New source files must be added to the reviewed manifest explicitly. File selection does not anonymize file contents; see [PRIVACY.md](PRIVACY.md) before publishing. Your original local files remain intact. If your development checkout contains private financial data in its history, start a fresh Git repository in the exported directory when publishing.

`prepare-release` exports source to a new directory, runs the tests there, and creates a source ZIP, SHA-256 checksum, and validation manifest. It does not exercise live YNAB writes.

No GitHub repository is created or pushed by these commands. Legacy personal scripts in the development checkout are not part of the public application; use the configurable `spending-report` and `correct` commands instead.

Licensed under MIT; see [LICENSE](LICENSE).
