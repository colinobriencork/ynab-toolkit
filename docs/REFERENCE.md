# Detailed behavior and configuration

Start with the [command guide, setup, and workflows](../README.md). This reference explains the rules behind the commands.

## Configuration

Run `ynab-toolkit setup` to save your YNAB token and create starter settings. An installed copy uses `~/.config/ynab-toolkit/config.toml` and the `.env` beside it; `XDG_CONFIG_HOME` replaces the `~/.config` base directory when set. Development copies use settings in their source checkout, as described in [Contributing](../CONTRIBUTING.md#use-your-development-copy-as-a-command).

Use `--config /path/to/settings.toml` to select a settings file, or set `YNAB_TOOLKIT_CONFIG` in your shell. The command-line option takes precedence. The matching `.env` is always read beside the selected file, so credentials from separate settings folders are not combined. An explicitly selected missing file is an error, except that `setup` can create it.

For manual configuration, set `YNAB_API_TOKEN` in `.env` or the process environment. Supported process environment values override `.env`, which overrides top-level TOML settings. `config.toml` is optional when the required settings are supplied through those variables.

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
backend = "claude"
# Optional provider-specific model and guess_model overrides.
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

Categorization reviews unapproved transactions in budget accounts, including ones YNAB has already assigned a category. Applying a confirmed category also approves the transaction. Approved transactions are left unchanged; tracking-account entries and account transfers are excluded.

```bash
ynab-toolkit categorize --budget "Household"
ynab-toolkit categorize --budget "Household" --apply
ynab-toolkit categorize --report reports/categorize-preview.json
ynab-toolkit categorize --backend codex --budget "Household"
```

Exact, case-insensitive merchant rules run first. Otherwise, a first-pass model uses transaction details and approved merchant history. Uncertain cases get a second pass with nearby transaction context and web research when supported by the backend. A transaction cannot serve as its own historical evidence. Group-qualified names and category IDs are supported; ambiguous names are rejected.

Transfers, splits, bare person-to-person transfers without a configured rule, and unresolved transactions are left for review. Confirmed model/rule decisions are approved when applied. Individual failures are recorded and the run continues; failed or unverifiable runs return a nonzero exit code. No interactive classification questions interrupt the run.

Budget and merchant details are sent to the selected model provider, including during previews. Research may send merchant information to web services. YNAB, Amazon, and email credentials are removed from subprocess environments; provider authentication remains available. Backend failures do not echo raw prompts or stderr into run journals. There is no automatic fallback to a different provider.

### Choosing a model backend

Set `backend` in `[defaults]` or a per-budget table, or use `--backend`. `--model` and `--guess-model` override the two passes. Changing providers through a CLI or per-budget override discards inherited model names. When editing the backend in the same TOML table, remove or replace its old model settings too.

| Backend | Models when omitted | Research in the second pass |
| --- | --- | --- |
| `claude` (default) | `haiku`, then `opus` | WebSearch/WebFetch |
| `codex` | Codex CLI default; an explicit `model` also applies to the second pass unless `guess_model` is set | Codex web search |
| `command` | Command's own default; explicit `model` is reused unless `guess_model` is set | Context only |

For Codex, install a current CLI and authenticate with `codex login`. This adapter was verified with CLI 0.153.4 and requires `exec --ignore-user-config` and `--ephemeral`. It reuses saved CLI authentication; a separate API key is unnecessary when that login provides Codex access. See [Codex noninteractive mode](https://developers.openai.com/codex/noninteractive).

```toml
[defaults]
backend = "codex"
timeout = 60
guess_timeout = 180
```

Codex runs in a temporary working directory with its read-only sandbox, ephemeral sessions, no inherited user config or project instructions, and shell, app, plugin, browser, computer, hook, and subagent capabilities disabled. First-pass web search is disabled; the second pass enables web search. This adapter intentionally ignores user-configured Codex model/provider defaults: use toolkit model options, or a trusted custom wrapper for a different Codex provider. Organization policy still applies. Ephemeral mode prevents saved session rollouts; it does not change the provider's data-retention policy.

Claude's first pass has no built-in tools; the second enables only WebSearch/WebFetch. These tool restrictions are not an OS sandbox, and installed Claude configuration remains relevant.

For another CLI, a local model, or an API wrapper, use an argument array:

```toml
[defaults]
backend = "command"
backend_command = ["/absolute/path/to/model-wrapper", "--model", "{model}"]
model = "your-model"
# Optional different command/model for the second pass:
# guess_backend_command = ["/absolute/path/to/review-wrapper"]
# guess_model = "your-review-model"
```

The command receives the complete prompt on stdin and must return the final response on stdout: `CATEGORY: <name>`, `QUESTION: <question>`, or `UNCERTAIN: <reason>`. Send diagnostics to stderr and exit nonzero on failure. Only a complete `{model}` argument is substituted; the toolkit performs no shell expansion and appends no implicit flags. Use absolute paths for scripts/config files because commands run in a temporary directory. Keep credentials in the wrapper's normal credential store or environment, never command arguments. A custom command is trusted executable code and must enforce its own tool permissions and privacy behavior.

Python integrations can supply an object implementing `LLMBackend` from `ynab_categorizer.backends`, or continue injecting the existing `run_llm` and `guess_llm` callbacks. Adding a native backend does not require changing category parsing or YNAB update logic.

### Optional Amazon enrichment

```bash
ynab-toolkit amazon-login --install-browser
```

First use of `amazon-login` prompts for and privately saves these fields; `--configure` prompts again. See the [authenticator key walkthrough](../README.md#finding-the-authenticator-setup-key). For manual configuration, set `AMAZON_USERNAME`, `AMAZON_PASSWORD`, `AMAZON_OTP_SECRET_KEY`, `AMAZON_DOMAIN`, and **`AMAZON_BUDGET_NAME`** in `.env`. Enrichment requires an explicit matching budget name so one person's orders are not used for another budget. `AMAZON_OTP_SECRET_KEY` is the authenticator's base32 secret, not a six-digit code.

If you already use an authenticator and have its setup key, reuse that key. Otherwise, use the app's supported secret/export feature or add another authenticator through Amazon's Two-Step Verification settings. Keep your working recovery method. A six-digit code cannot recover the setup key. See [Amazon's enrollment instructions](https://kdp.amazon.com/en_US/help/topic/G6HTFZJLJ7AJQ56R).

Save your normal Amazon password without a temporary code appended. The toolkit generates the code from the setup key and handles either an appended code or a separate code field. [Amazon's alternate sign-in method](https://digprjsurvey.amazon.co.uk/csad/help/node/201962400).

`--install-browser` installs the matching [Playwright Chromium browser](https://playwright.dev/python/docs/browsers). To sign in again, run `ynab-toolkit amazon-login`; add `--configure` to change saved details. Use `--install-browser` again if a toolkit update requires a newer browser version.

The browser profile is stored at `~/.config/ynab-categorizer/amazon-profile`. Expired sessions can open a login browser; a CAPTCHA or passkey challenge may require interaction. Parsing currently assumes English order dates and dollar-formatted Amazon totals; other marketplace formats are not supported automatically.

## Budget assignment and verification

```bash
ynab-toolkit assign --budget "Household"
ynab-toolkit phantom-assign --budget "Household"
ynab-toolkit phantom-assign --budget "Household" --apply
ynab-toolkit budget-check --budget "Household"
ynab-toolkit phantom-assign --budget "Household" --month 2030-02
```

`assign` spends only positive Ready to Assign. It funds carried-over card debt, current overspending, then targets in configured priority order. Nonpositive RTA is reported as unavailable cash, not as proof that all categories are funded.

`phantom-assign` uses RTA plus expected income to lay out the month. It funds carried-over card debt, current overspending, and committed targets in full, then distributes the remainder across the discretionary group. **Commitments can exceed expected income:** the report identifies that uncovered amount. Negative RTA is expected when the plan depends on future income; category balances are not proof that the cash has already arrived.

Preview JSON distinguishes `checks` for the starting snapshot (`checks_scope = "before_assignments"`) from `projection` for the proposed assignments. The projection reports cash remaining before and after expected income and identifies plans that depend on future income or remain uncovered. These monthly totals do not establish that cash arrives before individual bills are due.

`assign`, `phantom-assign`, and `budget-check` accept `--month YYYY-MM`; omitted, they use the current month. All snapshots, assignment writes, and verification stay in the selected month. Expected income includes only future payments in that month, with payroll freshness checked against today's date. Future-month plans are provisional: remaining spending and YNAB's month-end target rollover can change the required funding. Historical discretionary averages use the three completed months before today.

Without an explicit income schedule, the planner detects recent biweekly payroll. It rejects stale patterns after a missed expected payday and reduces the influence of an unusually large latest payment. This is a heuristic, not a guarantee; use explicit dated income for monthly, irregular, or changing pay.

All planners and checks share one target policy. With the default `spending_target_mode = "refill"`, ordinary monthly and weekly spending targets count existing funding, including carryover. For example, a monthly requirement of 80 and 25 carried forward needs 55 newly assigned. After spending begins, the calculation uses Available minus Activity, so money already spent against that month's allowance still counts. Weekly requirements count due weekdays in the selected month. Savings, Infrequent, configured saving/repayment groups, balance-building targets and longer-term targets retain YNAB's installment/contribution calculation. `accumulate_categories` adds explicit exceptions. Use `spending_target_mode = "ynab"` to preserve YNAB's own target behavior throughout. These are toolkit planning rules; they do not edit YNAB target definitions. JSON reports show each target's rule, requirement, existing funding and additional need.

Discretionary categories with positive targets use the same remaining-target calculation; categories without a positive target use recent average spending. Explicit `wants_overrides` take precedence. For the average/override fallback, `wants_mode = "refill"` counts carryover funding; `"accumulate"` adds this month's allowance independently of carryover. Ordinary targets skip hidden categories, while negative hidden spending balances are included in overspending coverage and checks. Proportional allocations respect the budget's currency decimal precision (optional `currency_decimal_digits` override, 0–3).

Review essential categories without targets or outside the configured priority groups: they may still need an allowance. A discretionary remainder alone does not establish that every necessary expense is covered.

Both commands share an application workflow:

1. Fetch a budget snapshot and calculate additions.
2. Check that the snapshot and affected assignments have not changed before writing.
3. Apply assignments, journaling each operation.
4. Fetch fresh card balances after YNAB moves money from spending categories.
5. Transfer surplus payment funding to underfunded cards, releasing the donor assignment first.
6. Read back actual assignments, RTA, remaining overspending, and card differences.

Card transfers do not send bank payments. A preview lists current-state possible transfers; the exact transfers are recalculated after funding. Changes are separate API requests, not an atomic transaction. A concurrent edit or failure stops further budgeting writes and returns a nonzero exit status. Run `budget-check` before retrying.

`budget-check` is read-only and reports RTA, expected income coverage, underfunded targets, overspending, and each card's surplus/shortfall. It returns nonzero for uncovered forecasts, overspending, or underfunded cards. Targets alone may remain underfunded intentionally. Use `--report PATH` for JSON.

## Rebalance unused month-end funding

```bash
ynab-toolkit rebalance --budget "Household" --report reports/rebalance.json
ynab-toolkit rebalance --budget "Household" --apply --report reports/rebalance-applied.json
```

`rebalance` moves existing available category funding to current-month overspending, using the same target intent as the planners. It never treats savings contributions or longer-term reserves as spare spending money. It preserves Ready to Assign and never sweeps more than the shortfalls require. It supports only the current month and refuses negative Ready to Assign. Use it after recording month-end spending: imports can lag, and an empty scheduled-transaction list does not establish that all bills are paid.

Savings, Infrequent, saving/repayment/excluded groups, accumulating categories, hidden categories, and categories with balance-building or longer-term targets are excluded as donors. Card payment balances are reserved for their cards; the existing reconciliation step only moves actual card surplus. Any category with an outgoing scheduled transaction still due this month is retained in full, including split expenses. Add `rebalance_protected_groups` and `rebalance_protected_categories` for other reserves, and `rebalance_keep` for minimum available balances in milliunits. Category selectors accept IDs, exact names or `Group: Category`; missing or ambiguous selectors stop the run. Protections affect donating, not receiving money to cover overspending.

Donors are used in Wants, Needs, Bills order, then other eligible groups. Recipients cover cash overspending first, then credit overspending, largest shortfalls first. `rebalance_last_categories` defers selected recipients within each tier, for example pending reimbursements. Credit purchases and refunds are counted by split parts, excluding tracking accounts. Funding current spending categories lets YNAB fund the card payment; the command does not also assign the same amount to the card. Carried-over card debt remains the job of `assign`.

Reports list every positive spending balance's retained/released amount and reason, each proposed transfer, remaining shortfalls, and the effect of reduced carryover on next month's target top-ups. The next-month comparison holds other spending and assignments constant; it is not a full forecast. Categories without positive targets still need separate spending allowances. Applying releases donors before funding recipients, rechecks donor balances and scheduled costs, journals each operation and verifies results. Interrupted writes may leave released money in Ready to Assign; inspect the journal and rerun `budget-check` before recovery. API requests are not atomic.

Releasing a category also records a private funding hold for that budget and month. `assign` and `phantom-assign` then skip its target/average top-ups for the rest of that month, while still covering any new overspending. Future months automatically count the actual remaining balance toward the target and have no hold. `--refill-rebalanced` overrides a hold for an intentional subsequent funding run. Holds live under `$XDG_STATE_HOME/ynab-categorizer/funding-holds` (default `~/.local/state`); they are local to this installation. A hold is saved before attempting a release so an uncertain API outcome cannot trigger an automatic refill. Restoring an assignment journal does not clear a hold; use the override if needed.

## Correct historical categorization

```bash
ynab-toolkit correct --budget "Household" --transaction TRANSACTION_ID --category "Essentials: Clothing"
ynab-toolkit correct --budget "Household" --transaction TRANSACTION_ID --category "Essentials: Clothing" --apply
```

For a single unsplit expense, this moves the transaction and the matching assignment from the original category to the destination in the transaction's month. It preserves approval state, checks available balances and RTA, then reconciles current card funding displaced by historical edits. Refunds, transfers, and splits require manual handling. The command refuses stale transaction data or ambiguous categories.

## Run journals and recovery

Applied runs automatically write private JSON journals to `$XDG_STATE_HOME/ynab-categorizer/runs` (default `~/.local/state/ynab-categorizer/runs`). `--report PATH` chooses a different location or saves a preview. Journals record identifiers, previous/proposed values, completed writes, and failures. They contain private financial information; they are not source files.

```bash
ynab-toolkit restore /path/to/run.json
ynab-toolkit restore /path/to/run.json --apply
```

Restore reverses completed operations only when current values still match what the run wrote. It refuses to overwrite later edits. Uncertain writes—such as a lost response after a request—require inspection before restoration. Keep journals intact. Restoration also consists of separate API calls and is itself journaled.

## Spending reports and email

```bash
ynab-toolkit spending-report --budget "Household" --start 2026-07-01 --end 2026-09-30 --report reports/spending.json
ynab-toolkit spend-watch --budget "Household"
ynab-toolkit spend-watch --send
```

Spending reports show category outflows, inflows, and net spending; income; and configured saving/repayment roles. Uncategorised account transfers are excluded, while categorized transfers to tracking accounts follow their category role. Split transactions are counted by their parts. Reports include hidden historical categories and flag unapproved/unknown categories and merchants using multiple categories. Flags are prompts to review, not proof of an error. Refunds and reimbursements are shown together as category inflows rather than guessed from payee names.

The report is a category summary with review flags, not a complete transaction ledger or an automatic explanation of every card shortfall. Inspect the underlying transactions in YNAB to trace a gap, and count reimbursements only when received.

`spend-watch` compares spending with each configured budget's `monthly_income` and thresholds. Optional `savings_pairs` maps comparison labels to category-name keywords. No savings comparisons run unless you configure this table; an empty table also disables them. Existing `MONTHLY_INCOME_<FIRST_WORD>` environment values remain supported in currency units; explicit per-budget `monthly_income` avoids first-word collisions and uses milliunits.

Email uses Gmail SMTP. Set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, and optional `SPEND_WATCH_RECIPIENTS`; email is sent only with `--send`. Reports reflect YNAB's recorded balances and categories, not independently reconciled bank statements. The CLI currently uses English labels and dollar-style displays; amounts belong to the selected budget's currency and are never converted.

## Contributing

For development setup, testing, editable installs, and public release preparation, see [CONTRIBUTING.md](../CONTRIBUTING.md).

Licensed under MIT; see [LICENSE](../LICENSE).
