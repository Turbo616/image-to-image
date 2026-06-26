#!/usr/bin/env python3
"""Push this project to GitHub using a personal access token.

Set GITHUB_TOKEN first, then run:
    python push_to_github_token.py
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


OWNER = "Turbo616"
REPO = "image-to-image"
BRANCH = "main"
COMMIT_MESSAGE = "Initial image matcher panel"

ROOT = Path(__file__).resolve().parent

FILES = [
    ".gitignore",
    "README.md",
    "app.py",
    "image_matcher.py",
    "synology_cloud.py",
    "requirements.txt",
    "launch_panel.bat",
    "open_panel.vbs",
    "start_panel.bat",
    "start_panel_hidden.vbs",
]

SKIP_DIRS = {"work", "results", "outputs", "vendor", "__pycache__"}


def api_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "image-matcher-token-uploader",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"https://api.github.com{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc


def ensure_main_branch(token: str) -> None:
    try:
        api_request("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", token)
        return
    except RuntimeError as exc:
        if "409" not in str(exc) and "404" not in str(exc):
            raise

    # Empty repositories have no branch yet. Creating the first file through
    # contents API creates the default branch automatically.


def get_existing_sha(path: str, token: str) -> str | None:
    try:
        data = api_request("GET", f"/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}", token)
        return data.get("sha")
    except RuntimeError as exc:
        if "404" in str(exc) or "409" in str(exc):
            return None
        raise


def upload_file(path: str, token: str) -> None:
    full_path = ROOT / path
    content = base64.b64encode(full_path.read_bytes()).decode("ascii")
    payload = {
        "message": f"{COMMIT_MESSAGE}: {path}",
        "content": content,
        "branch": BRANCH,
    }
    sha = get_existing_sha(path, token)
    if sha:
        payload["sha"] = sha
    api_request("PUT", f"/repos/{OWNER}/{REPO}/contents/{path}", token, payload)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is not set.")
        print("PowerShell example:")
        print('$env:GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"; python push_to_github_token.py')
        return 1

    ensure_main_branch(token)
    for path in FILES:
        if not (ROOT / path).is_file():
            print(f"Skip missing file: {path}")
            continue
        print(f"Uploading {path} ...")
        upload_file(path, token)

    print("Done.")
    print(f"https://github.com/{OWNER}/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
