# Changelog

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
