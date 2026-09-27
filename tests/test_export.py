import importlib.util
from pathlib import Path

import pytest


def exporter(root):
    spec = importlib.util.spec_from_file_location('export_public', Path(__file__).parents[1] / 'scripts/export_public.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    module.FILES = ('README.md', 'ynab_categorizer/public.py')
    return module


def test_public_export_excludes_private_data_and_history(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'README.md').write_text('Public documentation')
    for name in ('.env', 'config.toml', 'financial-report.md'):
        (root / name).write_text('private')
    for name in ('.git', 'output', '.claude'):
        (root / name).mkdir()
        (root / name / 'private.json').write_text('private')
    (root / 'ynab_categorizer').mkdir()
    (root / 'ynab_categorizer' / 'public.py').write_text('pass')
    (root / 'ynab_categorizer' / 'private_analysis.py').write_text('private financial data')
    (root / 'ynab_categorizer' / 'linked.py').symlink_to(root / '.env')
    dest = tmp_path / 'public'
    exporter(root).export_public(dest)
    assert sorted(str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()) == ['README.md', 'ynab_categorizer/public.py']


@pytest.mark.parametrize('linked_parent', [False, True])
def test_export_rejects_symlinked_manifest_paths_before_copy(tmp_path, linked_parent):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'README.md').write_text('Public')
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'public.py').write_text('private data')
    if linked_parent:
        (root / 'ynab_categorizer').symlink_to(private, target_is_directory=True)
    else:
        (root / 'ynab_categorizer').mkdir()
        (root / 'ynab_categorizer/public.py').symlink_to(private / 'public.py')
    dest = tmp_path / 'public'
    with pytest.raises(ValueError, match='symlink'):
        exporter(root).export_public(dest)
    assert not dest.exists()


def test_export_fails_closed_when_reviewed_source_is_missing(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'README.md').write_text('Public')
    dest = tmp_path / 'public'
    with pytest.raises(ValueError, match='Missing reviewed public file'):
        exporter(root).export_public(dest)
    assert not dest.exists()


def test_export_rejects_destination_inside_source(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        exporter(tmp_path).export_public(tmp_path / 'public')
