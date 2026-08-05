"""Root conftest.py.

Adds the project root to sys.path so that the
``examples`` package (and any other project-level modules) are importable
from within test files.

pytest automatically executes this file before collecting tests, so no
explicit import is needed.
"""

import subprocess  # noqa: S404
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
TEMPLATE = ROOT / ".env.test.template"
ENV_FILE = ROOT / ".env.test"


# Ensure the project root is on sys.path (required for `from examples import ...`)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):  # noqa: ARG001
    if not TEMPLATE.exists():
        return
    subprocess.run(
        ["op", "inject", "-i", str(TEMPLATE), "-o", str(ENV_FILE), "-f"],
        check=True,
    )
    load_dotenv(ENV_FILE, override=True)


def pytest_unconfigure(config):
    if ENV_FILE.exists():
        ENV_FILE.unlink()
