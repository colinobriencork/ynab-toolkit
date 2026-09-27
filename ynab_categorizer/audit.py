"""Private, atomic run journals. Never put credentials or complete API responses here."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def write_report(report: dict, path=None) -> str:
    """Write a recoverable JSON journal with owner-only permissions."""
    if path is None:
        root = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
        path = root / 'ynab-categorizer' / 'runs' / f'{uuid4()}.json'
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), **report}
    fd, temporary = tempfile.mkstemp(prefix='.run-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, indent=2, default=str)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)
