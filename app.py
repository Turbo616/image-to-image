#!/usr/bin/env python3
"""Launcher for the local image matcher panel.

The full app source is stored in compressed payload chunks so GitHub MCP uploads stay reliable.
"""

import base64
import gzip
from pathlib import Path

ROOT = Path(__file__).resolve().parent
payload = "".join((ROOT / f"app_payload_{i:02d}.txt").read_text(encoding="ascii").strip() for i in range(1, 7))
exec(compile(gzip.decompress(base64.b64decode(payload)), "app.py", "exec"))
