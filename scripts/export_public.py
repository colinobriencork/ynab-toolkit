"""Build a source-only folder without private exports, local settings, or Git history."""

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Explicitly reviewed file paths. New files require an intentional review here.
FILES = (
    'README.md',
    'docs/REFERENCE.md',
    'CHANGELOG.md',
    'LICENSE',
    'pyproject.toml',
    'pdm.lock',
    '.gitignore',
    '.env.example',
    'config.example.toml',
    'PRIVACY.md',
    '.github/workflows/tests.yml',
    'scripts/export_public.py',
    'scripts/prepare_release.py',
    'tests/__init__.py',
    'tests/test_amazon.py',
    'tests/test_backends.py',
    'tests/test_budget_workflow.py',
    'tests/test_budgeting.py',
    'tests/test_categorizer.py',
    'tests/test_cli.py',
    'tests/test_client.py',
    'tests/test_config.py',
    'tests/test_setup.py',
    'tests/test_export.py',
    'tests/test_operations.py',
    'tests/test_orchestrator.py',
    'tests/test_reporting.py',
    'tests/test_rebalance.py',
    'tests/test_spendwatch.py',
    'ynab_categorizer/__init__.py',
    'ynab_categorizer/__main__.py',
    'ynab_categorizer/amazon.py',
    'ynab_categorizer/audit.py',
    'ynab_categorizer/backends.py',
    'ynab_categorizer/budget_workflow.py',
    'ynab_categorizer/budgeting.py',
    'ynab_categorizer/categorizer.py',
    'ynab_categorizer/client.py',
    'ynab_categorizer/config.py',
    'ynab_categorizer/funding_holds.py',
    'ynab_categorizer/operations.py',
    'ynab_categorizer/orchestrator.py',
    'ynab_categorizer/reporting.py',
    'ynab_categorizer/rebalance.py',
    'ynab_categorizer/setup.py',
    'ynab_categorizer/spendwatch.py',
    'ynab_categorizer/target_policy.py',
    'ynab_toolkit/__init__.py',
    'ynab_toolkit/__main__.py',
)


def export_public(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Destination must be a new folder')
    # Never recurse into the destination or a private local directory.
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError('Choose a destination outside the working repository')
    # Validate the complete manifest before copying anything. Symlinked parents
    # can otherwise bring private files into an apparently allowlisted path.
    sources = []
    for name in FILES:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError('Public manifest paths must stay inside the source root')
        source = ROOT / relative
        if any((ROOT / Path(*relative.parts[:i])).is_symlink()
               for i in range(1, len(relative.parts) + 1)):
            raise ValueError(f'Public source must not be a symlink: {name}')
        if not source.is_file():
            raise ValueError(f'Missing reviewed public file: {name}')
        sources.append((source, relative))
    destination.mkdir(parents=True)
    for source, relative in sources:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f'Exported {len(sources)} allowlisted source files to {destination}. '
          'File selection does not anonymize contents; review fixtures before publishing.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    args = parser.parse_args()
    export_public(args.destination)
