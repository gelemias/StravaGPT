"""Test environment setup.

The MCP endpoint reads its configuration when ``app.main`` is imported (the
route and its transport-security settings are built at import time, mirroring
how environment variables work in production). These values therefore have to be
in place before any test module imports the app.
"""

from __future__ import annotations

import os
import tempfile

# TestClient sends "Host: testserver", which the MCP SDK's DNS rebinding
# protection rejects with 421 unless the host is allowed.
os.environ.setdefault("MCP_ALLOWED_HOSTS", "testserver")
os.environ.setdefault("SYNC_ON_STARTUP", "false")
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.join(tempfile.mkdtemp(prefix="stravagpt-tests-"), "test.db"),
)
# Tests must never talk to the real Intervals.icu API.
os.environ.pop("INTERVALS_API_KEY", None)
