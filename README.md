# YNAB Toolkit

Categorize transactions, plan your budget, cover overspending, and review spending from the command line.

## Command guide

```text
ynab-toolkit COMMAND [options]
```

| Command | What it does |
| --- | --- |
| `categorize` | Review categories for unapproved transactions, including ones YNAB categorized automatically. |
| `assign` | Budget money you already have available. |
| `phantom-assign` | Plan a month using expected income. |
| `rebalance` | Move unused money this month to cover overspending. |
| `spend-watch` | Compare spending with your configured income. |
| `budget-check` | Check overspending, targets, and credit card funding. |
| `correct` | Fix one expense's category and matching funding. |
| `restore` | Reverse supported changes from a saved run journal. |
| `spending-report` | Analyze spending over a date range. |
| `setup` | Save your YNAB token and create starter settings. |
| `amazon-login` | Save an Amazon session for purchase matching. |

```bash
ynab-toolkit --help
ynab-toolkit rebalance --help
```

**Budget changes preview by default. Add `--apply` to make them.** Reports and checks are read-only; `spend-watch --send` explicitly sends email. `setup` saves local settings, and `amazon-login` saves a browser session.

Common options are `--budget "Your Budget Name"`, `--config /path/to/config.toml`, and `--report reports/run.json` where supported. `assign`, `phantom-assign`, and `budget-check` accept `--month YYYY-MM`. Each command's `--help` lists its exact options.

[Setup](#setup) · [When to use each command](#a-practical-budgeting-workflow) · [Detailed rules and configuration](docs/REFERENCE.md) · [Contributing](CONTRIBUTING.md)

## Setup

Install the command, connect your YNAB account, and choose the optional integrations you want to use. To change the toolkit's code, see [Contributing](CONTRIBUTING.md).

### 1. Install the command

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv tool install --python 3.13 git+https://github.com/colinobriencork/ynab-toolkit.git
ynab-toolkit --help
```

This installs the toolkit and its Python dependencies in an isolated environment and puts `ynab-toolkit` on your PATH. If uv reports that its executable directory is missing from PATH, run `uv tool update-shell` and open a new terminal. See [uv's tool installation guide](https://docs.astral.sh/uv/guides/tools/#installing-tools).

To update later:

```bash
uv tool upgrade ynab-toolkit
```

Python 3.13 or newer is required; the install command selects that version for you. AI CLIs and the optional Amazon browser are configured separately below.

### 2. Connect YNAB

Get a Personal Access Token using [YNAB's official instructions](https://api.ynab.com/#personal-access-tokens). In the web app, open Account Settings → Developer Settings, find Personal Access Tokens, and generate a token for your own account. Then run:

```bash
ynab-toolkit setup
```

Setup opens [YNAB's Developer Settings](https://app.ynab.com/settings/developer) and asks you to paste the token into a hidden prompt. On first setup, it also asks for an optional default budget name and your AI backend. It saves everything for you; **you do not need to create a credentials file by hand**. On macOS and Linux, an installed copy stores its private settings here:

```text
~/.config/ynab-toolkit/.env
~/.config/ynab-toolkit/config.toml
```

Setup prints the exact paths it used. Token files created by setup are readable and writable only by your user on systems with Unix file permissions. Keep your token private; it grants access to your YNAB account.

You can later adjust budgeting preferences in `config.toml`. For example, choosing a budget and Codex during setup produces settings like:

```toml
default_budget = "Your Budget Name"

[defaults]
backend = "codex"
spending_target_mode = "refill"
```

Without `default_budget`, select a budget at the prompt or supply `--budget`. The [full example configuration](config.example.toml) shows optional priorities, protected balances, income schedules, and merchant rules. All monetary settings use **milliunits: 1000 = 1 currency unit**.

For custom settings locations and environment variables, see the [configuration reference](docs/REFERENCE.md#configuration).

### 3. Choose an AI backend for categorization

Only `categorize` needs AI. Budget planning, rebalancing, corrections, checks, and spending reports work without it.

| Backend | Install and sign in | Toolkit setting |
| --- | --- | --- |
| Codex | Follow [OpenAI's Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli), then run `codex login`. | `backend = "codex"` |
| Claude Code | Follow [Anthropic's setup instructions](https://code.claude.com/docs/en/setup), then run `claude` and complete sign-in. | `backend = "claude"` |
| Another CLI or local model | Provide a trusted command that reads a prompt from stdin and writes its answer to stdout. See [custom backends](docs/REFERENCE.md#choosing-a-model-backend). | `backend = "command"` |

Choose the backend during `setup`, or change `backend` under `[defaults]` in `config.toml` later. The toolkit reuses the CLI's existing authentication; you do not paste that provider's token into the toolkit. A Codex login with access does not require a separate OpenAI API key.

Try a preview:

```bash
ynab-toolkit categorize --backend codex
```

The toolkit sends transaction and merchant details to your chosen AI provider, including during previews. It can also research merchants in a second pass. There is no automatic switch to a different provider. Model defaults, permissions, and custom command requirements are explained in the [backend reference](docs/REFERENCE.md#choosing-a-model-backend).

### 4. Optional: connect Amazon

Connect Amazon so the toolkit can use order details to categorize your purchases.

#### Finding the authenticator setup key

1. In Amazon, open **Login & security → Two-Step Verification** and add an **Authenticator App**.
2. At the QR code, choose the manual-entry option, usually **Can't scan the barcode?** Copy the long setup key.
3. Add the same key or QR code to your authenticator app. Enter the app's current six-digit code on Amazon to finish enrollment.

Keep the setup key private. **The toolkit needs that long key, not a temporary six-digit code.** [Amazon's illustrated guide](https://m.media-amazon.com/images/G/01/AGS/SEA/2SV_Guide_ASVN._CB1535016505_.pdf).

#### Connect the toolkit

```bash
ynab-toolkit amazon-login --install-browser
```

Enter your Amazon login, normal password, setup key, Amazon domain (such as `amazon.ca`), and YNAB budget name. Finish signing in through the browser that opens. Your details and session are saved locally for later use.

Order matching supports English dates and dollar-formatted totals. For existing authenticators, changing saved details, or signing in again, see the [Amazon reference](docs/REFERENCE.md#optional-amazon-enrichment).

### 5. Check the connection

```bash
ynab-toolkit budget-check
```

This reads your budget and reports its condition without changing it. An overspending or card-funding warning means there is something to review, not necessarily that setup failed.

## A practical budgeting workflow

Use the commands in this order as needed. **Every amount, merchant, and scenario below is invented.** Examples describe the results to expect, not literal terminal output. Dates are placeholders to replace with the month you are reviewing.

### When transactions arrive: categorize, then check

```bash
ynab-toolkit categorize --report reports/categories-preview.json
ynab-toolkit categorize --apply --report reports/categories-applied.json
ynab-toolkit budget-check
```

Review the preview before applying. Categorization checks **unapproved on-budget transactions**, including ones YNAB already categorized. Applying a confirmed choice also approves it. Already-approved transactions are left alone. Transfers, tracking-account entries, splits, and unresolved cases are left for manual review.

For example, an unapproved 8-unit purchase at Fictional Stationery Store may have been guessed as Groceries. A configured merchant rule or the AI can propose Hobbies instead. Applying changes its category and approves it; a later categorization run skips it.

If a merchant always belongs in one category, add a rule under its budget in `config.toml`:

```toml
[budgets."Your Budget Name"]
merchant_rules = { "Fictional Stationery Store" = "Wants: Hobbies" }
```

### When a card shortfall looks surprising: investigate before funding it

```bash
ynab-toolkit budget-check --report reports/health.json
ynab-toolkit spending-report --start 2030-01-01 --end 2030-01-31 --report reports/spending.json
```

`budget-check` shows how much is owed on each card and how much is available in its payment category. `spending-report` shows category outflows, inflows, net spending, and review flags. The JSON also identifies merchants appearing in multiple categories and transaction IDs for flagged items. Use those clues to inspect the relevant transactions in YNAB; this command is not a complete transaction-ledger export or automatic explanation of every card gap.

For example, a work expense of 80 followed by a reimbursement of 20 leaves 60 uncovered. An unrelated reimbursement received and passed on to somebody else is not additional money for that expense. The report shows recorded category inflows; it does not guess which future claims will be paid. Check the underlying transactions before treating a shortfall as reimbursable.

A current-month credit purchase shortfall is generally covered in its spending category, allowing YNAB to move funding to the card. Older debt carried into a later month needs card-payment funding. The planners distinguish these so the same gap is not funded twice.

### After payday: assign money that has arrived

```bash
ynab-toolkit assign --report reports/assign-preview.json
ynab-toolkit assign --apply --report reports/assign-applied.json
ynab-toolkit budget-check
```

`assign` uses positive Ready to Assign, funds carried-over card debt and overspending, then follows your category priorities and targets. It does not forecast a paycheck to make today's allocation fit.

For example, with 90 available and a 20 card gap, funding that gap leaves up to 70 for the remaining priorities. If money runs out, the remaining needs stay visible. The default priority groups are Bills, Needs, Savings, and Wants; customize them to match your own budget.

### Near month-end: rebalance unused money

First make sure recent transactions have imported and consider expenses still due. Then:

```bash
ynab-toolkit rebalance --report reports/rebalance-preview.json
ynab-toolkit rebalance --apply --report reports/rebalance-applied.json
ynab-toolkit budget-check
```

For example, Hobbies has 12 available and Dining is 9 overspent. If Hobbies is an eligible donor, rebalancing moves 9 to Dining and leaves 3 in Hobbies. The report lists **what moved from where to where**, the balances kept and why, any remaining shortfalls, and how reduced carryover changes next month's target funding.

Savings, accumulating reserves, longer-term targets, card-payment money, and categories with outstanding scheduled expenses are protected. You can add protections or minimum balances:

```toml
[budgets."Your Budget Name"]
rebalance_protected_categories = ["Bills: Upcoming service"]
rebalance_keep = { "Needs: Transport" = 5000 }
rebalance_last_categories = ["Needs: Reimbursements"]
```

Here, Transport keeps at least 5 units. A reimbursement category is considered last within its cash/credit priority tier. These settings must name real categories in your budget; they do not create categories.

Rebalancing covers cash overspending first, then credit overspending. It preserves Ready to Assign and only moves what is needed. It operates on the **current month only** and refuses negative Ready to Assign. It also records a local hold so `assign` does not immediately refill the categories you just released. Next month has no such hold. See [the rebalance rules](docs/REFERENCE.md#rebalance-unused-month-end-funding) for exceptions and recovery.

### Before next month: preview a plan using expected income

```bash
ynab-toolkit phantom-assign --month 2030-02 --report reports/next-month-preview.json
```

This combines Ready to Assign with expected income for the selected month. It covers card debt, overspending, and committed targets, then distributes the remainder within your discretionary group. Without an explicit income schedule, it looks for recent biweekly payroll; configure dated income if your pay is irregular or follows another cadence.

Existing funding counts toward ordinary monthly and weekly spending targets. For example, an 80-unit target with 25 carried forward needs **55 newly assigned**. Money already spent against that month's allowance still counts, preventing repeated refills. Savings contributions and accumulating reserves can intentionally need another contribution. These are toolkit calculations; YNAB's target definitions are not edited.

For a toy forecast with 120 total resources, 60 of committed needs and 20 of card debt leave 40 for discretionary allocations. That arithmetic can be correct while the plan is incomplete: a necessary category without a target or outside the priority groups may still need an allowance.

**The current discretionary planner uses positive targets where present, otherwise recent spending averages, and proportionally fits those amounts into the remaining money.** It can partially fund targets. The resulting remainder is not proof that every essential expense has been covered. Review missing targets, subscription changes, reimbursements, and bill timing before treating it as freely spendable.

Keep this as a preview while reviewing. Adding `--apply` writes the forecast's assignments now and can make Ready to Assign negative until the expected income arrives. Monthly totals do not prove that a paycheck arrives before an early-month bill. Use `assign` after income arrives if you want to allocate only existing money.

### Periodically: review spending against income

```bash
ynab-toolkit spend-watch
```

Configure `monthly_income`, `warn_ratio`, and `near_limit_ratio` for each budget first. For example, 45 spent against a configured income of 100 is 45%. This is a quick pacing check; use `spending-report` for category detail.

Optional email requires `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and optionally `SPEND_WATCH_RECIPIENTS` in your private `.env`. Only `ynab-toolkit spend-watch --send` sends it. See [reporting and email](docs/REFERENCE.md#spending-reports-and-email).

### When you find an older mistake: correct or restore

To fix one unsplit expense, use its transaction ID and an unambiguous category name:

```bash
ynab-toolkit correct --transaction TRANSACTION_ID --category "Needs: Clothing"
ynab-toolkit correct --transaction TRANSACTION_ID --category "Needs: Clothing" --apply
```

For example, correcting a 6-unit expense from Hobbies to Clothing also moves its matching assignment in the transaction's month, subject to balance checks. Approval status is preserved. This is useful for an already-approved expense that `categorize` intentionally skips. Refunds, splits, and transfers require manual handling.

Applied budget changes save a private journal automatically. To reverse supported completed changes from a particular run:

```bash
ynab-toolkit restore reports/assign-applied.json
ynab-toolkit restore reports/assign-applied.json --apply
```

Restore checks that the current values still match the run's writes; it refuses to overwrite later edits. It is not a universal undo for all subsequent YNAB activity. If a run stops partway through, inspect its journal and run `budget-check` before retrying. Operations use separate API requests, not an atomic transaction. See [journals and recovery](docs/REFERENCE.md#run-journals-and-recovery).

## More detail

- [Detailed behavior and configuration](docs/REFERENCE.md): target calculations, income assumptions, AI permissions, protections, verification, and recovery.
- [Example settings](config.example.toml) and [secret variable names](.env.example).
- [Privacy and publishing](PRIVACY.md): source exports exclude credentials, personal records, and Git history; contents still need review.
- [Changelog](CHANGELOG.md).

Reports describe the transactions recorded in YNAB, not independently reconciled bank statements. CLI amounts use the budget's currency with dollar-style displays; no currency conversion is performed. Keep reports and journals private.

## Contributing

To change the toolkit, follow the [contributor guide](CONTRIBUTING.md): clone or fork the repository, install the development environment, edit the code, run tests, and submit a pull request. It also covers editable command installs and preparing public source exports.

Licensed under [MIT](LICENSE).
