"""pytest bootstrap: make the repo-root fleet module (mcp_auth) importable
regardless of how pytest is invoked (python -m pytest from the repo root
already puts the root on sys.path; a bare pytest tests/ does not).

The app package itself resolves through pyproject's
[tool.pytest.ini_options] pythonpath = ["src", "."] — this bootstrap only
defends the top-level "import mcp_auth" for bare pytest invocations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
