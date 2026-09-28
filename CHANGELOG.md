# Changelog

## 0.2.0

- Install `ynab-toolkit` directly on PATH with uv or pipx; keep PDM optional for development.
- Prompt for YNAB and Amazon credentials, hide secret input, and save private settings outside installed package files. Preserve existing repository settings and support explicit settings paths.
- Lead the README with a command guide, then setup and practical budgeting workflows with invented examples; retain detailed rules in a separate reference.
- Add the `ynab-toolkit` PDM command and `python -m ynab_toolkit` entry point; keep the original module and command shortcuts compatible.
- Distinguish starting-balance checks from proposed funding status in previews, so a forecast depending on future income is not mistaken for a cash-backed plan.
- Add month-end `rebalance` with protected reserves, minimum balances, scheduled-expense checks, cash-first funding, and verified application journals.
- Share target intent across assignment, forecasting, checks and rebalancing: regular spending targets count carryover by default; contributions and longer-term reserves keep their installment behavior. `spending_target_mode = "ynab"` preserves the previous target calculation.
- Use positive discretionary targets before historical averages, and prevent same-month automatic refilling of released categories with private month-specific funding holds.
- Add `--month YYYY-MM` to budget planning and checks, with month-specific income forecasts, writes, and verification.
- Exclude tracking-account entries and account transfers from categorization; continue reviewing unapproved transactions while preserving approved decisions.
- Add selectable Claude, Codex, and custom-command categorization backends.
- Reuse Codex CLI authentication with ephemeral, read-only categorization runs.
- Support provider-specific model overrides without inheriting another provider's model names.
- Keep subprocess prompts on stdin and omit private backend diagnostics from run reports.
- Preserve preview-by-default behavior and the existing callback integration interface.

## 0.1.1

- Replace inherited financial fixtures with invented toy amounts and fictional descriptions.
- Make savings comparison categories opt-in through configuration.
- Export only explicitly reviewed file paths; reject symlinked or missing source files.
- Document review of fixture provenance and all published history.

## 0.1.0

Initial public source release.

- Preview-first CLI for categorization, assignment, correction, and restoration.
- Per-budget configuration for priorities, income forecasts, merchant rules, model selection, and spending reports.
- Shared post-apply checks and card payment funding redistribution.
- Guarded historical corrections and private operation journals with restoration.
- Explicit and stale-aware income forecasting; carryover-aware discretionary funding.
- Read-only budget checks and spending reports.
- Regression tests for ambiguous categories, stale data, interrupted writes, recovery, and card funding.
- Clean public export that excludes development budget data and history.

Migration: `categorize` now requires `--apply` to approve transactions. Existing `.env` credentials remain supported. Configure `AMAZON_BUDGET_NAME` explicitly to enable order enrichment.

Known limits: commands use separate YNAB API requests rather than atomic transactions. Automatic income detection supports biweekly patterns; other pay schedules require explicit dates. Amazon parsing currently assumes English dates and dollar-formatted totals. CLI displays use English and dollar-style amounts without currency conversion.
