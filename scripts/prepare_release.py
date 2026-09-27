"""Export and test clean source, then create a checksummed source archive."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from export_public import export_public


def prepare_release(destination):
    root = Path(destination).resolve()
    if root.exists():
        raise ValueError('Choose a new release destination')
    source = root / 'ynab-toolkit-0.1.1'
    export_public(source)
    # Capture the allowlisted payload before testing generates caches.
    paths = sorted(p for p in source.rglob('*') if p.is_file())
    subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=source, check=True)
    archive = root / 'ynab-toolkit-0.1.1.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, Path(source.name) / path.relative_to(source))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n')
    (root / 'release-validation.json').write_text(json.dumps({
        'version': '0.1.1', 'python': sys.version.split()[0],
        'source_tests': 'passed', 'files': len(paths),
        'archive': archive.name, 'sha256': digest,
        'live_write_validation': 'pending dedicated test budget',
        'published': False,
    }, indent=2) + '\n')
    print(f'Release source: {source}')
    print(f'Archive: {archive}')
    print(f'SHA256: {digest}')
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    prepare_release(parser.parse_args().destination)
