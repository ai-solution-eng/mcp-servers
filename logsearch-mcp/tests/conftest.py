"""pytest bootstrap: make `import server` work regardless of how pytest is
invoked (python -m pytest from the package root already puts the root on
sys.path; a bare `pytest tests/` does not)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
