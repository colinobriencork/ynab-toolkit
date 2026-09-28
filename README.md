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

Follow one small budget from January into February. **All names and amounts are invented.** Use your own dates and categories when running these commands.

### 1. Review new transactions

```bash
ynab-toolkit categorize
```

An unapproved $8 purchase at Fictional Stationery Store is in Groceries. The toolkit suggests Hobbies instead. Review the suggestions, then apply them:

```bash
ynab-toolkit categorize --apply
```

The transaction moves to Hobbies and is approved. Categorization also reviews YNAB's automatic choices; already-approved transactions are skipped.

### 2. See what needs attention

```bash
ynab-toolkit budget-check
ynab-toolkit spending-report --start 2030-01-01 --end 2030-01-31
```

The check flags Dining as $9 overspent. The spending report shows $29 spent there; you had assigned $20. The check also highlights any credit cards without enough money set aside for payment.

### 3. Cover overspending with spare money

Once the month's remaining expenses are accounted for, preview a rebalance:

```bash
ynab-toolkit rebalance
```

Hobbies has $12 spare, so the toolkit proposes moving $9 to Dining:

| Category | Available before | Available after |
| --- | ---: | ---: |
| Hobbies | $12 | $3 |
| Dining | −$9 | $0 |
| Savings | $50 | $50 |

Savings stays protected. The report explains each proposed move and which balances are being kept. Review it, then apply:

```bash
ynab-toolkit rebalance --apply --report reports/rebalance.json
```

This saves a record of what changed. Rebalancing works on the current month; [protection settings](docs/REFERENCE.md#rebalance-unused-month-end-funding) let you reserve other balances too.

### 4. Preview next month's budget

```bash
ynab-toolkit phantom-assign --month 2030-02
```

Transport has a monthly spending target of $80 and $25 left to carry forward. The plan calls for **$55 more**, bringing it to $80. It uses existing balances and expected income to work out the month's assignments.

Check that essential categories are funded and paychecks arrive before bills are due. This command previews by default; applying a forecast can make Ready to Assign negative until the income arrives. See [how targets and income are calculated](docs/REFERENCE.md#budget-assignment-and-verification).

### 5. Assign money after payday

When a $100 paycheck arrives in Ready to Assign:

```bash
ynab-toolkit assign
```

The toolkit proposes assignments using the money now available, your targets, and your priorities. Review them, then apply and check the result:

```bash
ynab-toolkit assign --apply
ynab-toolkit budget-check
```

### Other useful commands

`spend-watch` is read-only. `correct` and `restore` preview changes; add `--apply` after reviewing.

| When | Command | Example |
| --- | --- | --- |
| Check spending pace | `ynab-toolkit spend-watch` | With configured monthly income of $100 and spending of $45, shows 45% spent. |
| Fix an older, approved expense | `ynab-toolkit correct --transaction TRANSACTION_ID --category "Needs: Clothing"` | Moves a $6 expense from Hobbies to Clothing, along with its funding. |
| Undo a saved run | `ynab-toolkit restore reports/rebalance.json` | Reverses the recorded rebalance, provided later edits do not conflict. |

See the reference for [spending checks](docs/REFERENCE.md#spending-reports-and-email), [corrections](docs/REFERENCE.md#correct-historical-categorization), and [restoring changes](docs/REFERENCE.md#run-journals-and-recovery).

## More detail

- [Detailed behavior and configuration](docs/REFERENCE.md): target calculations, income assumptions, AI permissions, protections, verification, and recovery.
- [Example settings](config.example.toml) and [secret variable names](.env.example).
- [Privacy and publishing](PRIVACY.md): source exports exclude credentials, personal records, and Git history; contents still need review.
- [Changelog](CHANGELOG.md).

Reports describe the transactions recorded in YNAB, not independently reconciled bank statements. CLI amounts use the budget's currency with dollar-style displays; no currency conversion is performed. Keep reports and journals private.

## Contributing

To change the toolkit, follow the [contributor guide](CONTRIBUTING.md): clone or fork the repository, install the development environment, edit the code, run tests, and submit a pull request. It also covers editable command installs and preparing public source exports.

Licensed under [MIT](LICENSE).
