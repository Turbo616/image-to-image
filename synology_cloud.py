from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests


@dataclass(frozen=True)
class SynologyFile:
    path: str
    name: str
    file_id: str = ""


class SynologyClient:
    def __init__(self, server_url: str) -> None:
        server_url = server_url.strip().rstrip("/")
        if not server_url.startswith(("http://", "https://")):
            server_url = "http://" + server_url
        self.server_url = server_url
        self.session = requests.Session()
        self.sid = ""

    def _url(self, api_path: str) -> str:
        return urljoin(self.server_url + "/", "webapi/" + api_path)

    def _json(self, response: requests.Response, action: str) -> dict:
        text = response.text.strip()
        try:
            return response.json()
        except ValueError as exc:
            snippet = text[:180] if text else "empty response"
            raise RuntimeError(
                f"Synology returned a web page instead of data while {action}. "
                f"Response starts with: {snippet}"
            ) from exc

    def login(self, username: str, password: str) -> None:
        response = self.session.get(
            self._url("auth.cgi"),
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": username,
                "passwd": password,
                "session": "SynologyDrive",
                "format": "sid",
            },
            timeout=30,
        )
        data = self._json(response, "logging in")
        if not data.get("success"):
            code = data.get("error", {}).get("code", "unknown")
            if code == 400:
                raise RuntimeError("Synology login failed: account or password is incorrect.")
            if code == 401:
                raise RuntimeError("Synology login failed: this account is disabled.")
            if code == 403:
                raise RuntimeError("Synology login failed: 2-step verification may be required.")
            raise RuntimeError(f"Synology login failed. Error code: {code}")
        self.sid = data["data"]["sid"]

    def logout(self) -> None:
        if not self.sid:
            return
        self.session.get(
            self._url("auth.cgi"),
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "logout",
                "session": "SynologyDrive",
                "_sid": self.sid,
            },
            timeout=10,
        )
        self.sid = ""

    def _entry(self, params: dict, action: str) -> dict:
        params = dict(params)
        params["_sid"] = self.sid
        response = self.session.get(self._url("entry.cgi"), params=params, timeout=60)
        data = self._json(response, action)
        if not data.get("success"):
            code = data.get("error", {}).get("code", "unknown")
            if code == 408:
                raise RuntimeError("Synology session expired. Please search again.")
            if code in {414, 105, 1000, 1002, 1003}:
                raise RuntimeError("Synology folder not found. Please check the cloud folder path.")
            if code in {407, 417}:
                raise RuntimeError("This Synology account does not have permission to read that folder.")
            raise RuntimeError(f"Synology request failed while {action}. Error code: {code}")
        return data.get("data", {})

    def list_images(self, folder_path: str, extensions: set[str]) -> list[SynologyFile]:
        return self.list_drive_images(folder_path, extensions)

    def drive_path_candidates(self, folder_path: str) -> list[str]:
        folder_path = normalize_remote_path(folder_path)
        candidates = [folder_path]
        lower = folder_path.lower()
        if not lower.startswith(("/team-folders", "/mydrive", "/shared-with-me")):
            candidates.append("/team-folders" + folder_path)
        return list(dict.fromkeys(candidates))

    def list_drive_images(self, folder_path: str, extensions: set[str]) -> list[SynologyFile]:
        last_error: Exception | None = None
        for root_path in self.drive_path_candidates(folder_path):
            found: list[SynologyFile] = []
            stack = [root_path]
            try:
                while stack:
                    current = stack.pop()
                    data = self._entry(
                        {
                            "api": "SYNO.SynologyDrive.Files",
                            "version": "2",
                            "method": "list",
                            "filter": "{}",
                            "sort_direction": "asc",
                            "sort_by": "name",
                            "offset": "0",
                            "limit": "1000",
                            "path": current,
                        },
                        f"listing Drive folder {current}",
                    )
                    for item in data.get("items", data.get("files", [])):
                        item_path = item.get("path") or item.get("display_path") or ""
                        item_name = item.get("name") or Path(item_path).name
                        file_id = str(item.get("file_id") or item.get("id") or "")
                        item_type = str(item.get("type") or "").lower()
                        is_dir = bool(item.get("isdir")) or item_type in {"dir", "folder"}
                        if is_dir:
                            if file_id:
                                stack.append(f"id:{file_id}")
                            elif item_path:
                                stack.append(item_path)
                        elif Path(item_name).suffix.lower() in extensions:
                            found.append(SynologyFile(path=item_path, name=item_name, file_id=file_id))
                return found
            except Exception as exc:
                last_error = exc
                continue
        raise last_error or RuntimeError("Drive folder not found.")

    def download(self, file: SynologyFile | str, output_path: Path) -> None:
        if isinstance(file, SynologyFile):
            if not file.file_id:
                raise RuntimeError(f"Synology Drive did not return a file id for {file.name}")
            return self.download_drive_file(file.file_id, file.name, output_path)
        remote_path = file
        if remote_path.startswith("id:"):
            return self.download_drive_file(remote_path[3:], output_path.name, output_path)
        raise RuntimeError("Synology Drive download requires a cloud file id.")

    def download_drive_file(self, file_id: str, file_name: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.session.get(
            self._url("entry.cgi/" + (file_name or output_path.name)),
            params={
                "api": "SYNO.SynologyDrive.Files",
                "version": "2",
                "method": "download",
                "files": f'["id:{file_id}"]',
                "force_download": "true",
                "json_error": "true",
                "_sid": self.sid,
            },
            timeout=120,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            data = self._json(response, f"downloading Drive file {file_name}")
            code = data.get("error", {}).get("code", "unknown")
            raise RuntimeError(f"Synology Drive download failed. Error code: {code}")
        if not response.content:
            raise RuntimeError(f"Synology Drive returned an empty file while downloading {file_name}")
        output_path.write_bytes(response.content)


def normalize_remote_path(path: str) -> str:
    path = path.strip().strip('"').strip("'").replace("\\", "/")
    if path.startswith("http://") or path.startswith("https://"):
        raise ValueError(
            "Please enter a Synology folder path, not a browser URL. "
            "Example: /team-folders/Business/Design"
        )
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    return path.rstrip("/") or "/"
