# Contributing to YNAB Toolkit

This guide is for changing the toolkit's code or documentation. To use it with your budget, follow [Setup in the README](README.md#setup).

## Get a development checkout

Install Python 3.13 or newer and PDM. Fork the repository first if you do not have write access, then clone your fork; otherwise:

```bash
git clone https://github.com/colinobriencork/ynab-toolkit.git
cd ynab-toolkit
pdm install -G test
pdm run test
```

The tests use fake clients, HTTP mocks, and invented financial data. They do not require a YNAB token, an AI subscription, Amazon credentials, or a live budget.

## Make and check a change

```bash
git switch -c describe-your-change
pdm run ynab-toolkit --help
```

Edit the source or documentation, then run checks appropriate to the change. For a code change, run the relevant tests; run the complete suite before submitting it:

```bash
pdm run test
```

If you change installation or packaging, also build the package and check the installed command from outside the source directory:

```bash
pdm build
```

GitHub Actions runs tests on Python 3.13 and 3.14 and checks the installed command outside the checkout. Documentation-only changes normally need link and example checks rather than new tests.

Open a pull request explaining what changes for the user and how you checked it. Keep the command help, README examples, and [detailed behavior reference](docs/REFERENCE.md) consistent with any changed behavior.

## Use your development copy as a command

You can run the development copy with PDM:

```bash
pdm run ynab-toolkit budget-check
```

Or, from the checkout, install it with uv in editable mode:

```bash
uv tool install --editable .
ynab-toolkit --help
```

This points the command at your working copy, so source edits take effect without reinstalling. It is useful when developing the toolkit while also using it. A regular user's GitHub installation stays separate from source edits; the [README installation](README.md#1-install-the-command) is the normal user path.

The development copy reads `config.toml` and `.env` from the source checkout. Only configure real credentials if you intentionally want to use it with your account; [setup](README.md#2-connect-ynab) and the normal preview/apply rules still apply. To share those settings with a regular installed command, pass `--config /path/to/your/config.toml` or set `YNAB_TOOLKIT_CONFIG` to that path. The matching `.env` supplies its credentials. See the [configuration reference](docs/REFERENCE.md#configuration) for details.

`python -m ynab_toolkit ...` works with the project environment active. The command also accepts the spelling `ynab_toolkit`. The original `python -m ynab_categorizer ...` entry point, Python integration package, and existing journal/browser-profile directories retain their names for compatibility.

## Keep contributions free of personal records

Use small, invented amounts, placeholder IDs, and fictional merchants in tests and examples. Do not copy or scale real transactions into fixtures. Credentials, private settings, financial reports, browser profiles, and run journals stay out of Git. See [PRIVACY.md](PRIVACY.md) for the full publishing policy.

When adding a public source file, include it in the reviewed manifest in `scripts/export_public.py`. If it belongs in an installable package, update the package's build configuration too.

## Maintainers: export and prepare a release

Choose a new destination outside the working repository for each command:

```bash
pdm run export-public /tmp/ynab-public
pdm run prepare-release /tmp/ynab-release
```

`export-public` creates a source-only directory from the explicit manifest. It excludes private settings, reports, journals, browser profiles, personal scripts, and Git history. It selects files; it does not anonymize their contents. Review the actual export before publishing.

`prepare-release` exports the source, runs its tests, and creates a source ZIP, SHA-256 checksum, and validation manifest using the version in `pyproject.toml`. It does not exercise live YNAB writes. Use `pdm build` for an installable wheel and source distribution, and inspect those artifacts as well.

Neither command creates a GitHub repository or pushes code. If a development checkout has private financial data in its history, publish from a clean, reviewed repository rather than pushing that history.
