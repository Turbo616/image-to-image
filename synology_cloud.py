from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from urllib.parse import urljoin

import requests


@dataclass(frozen=True)
class SynologyFile:
    path: str
    name: str
    file_id: str = ""


class SynologyClient:
    CONNECT_TIMEOUT = 10
    LOGIN_READ_TIMEOUT = 180
    LIST_READ_TIMEOUT = 240
    DOWNLOAD_READ_TIMEOUT = 180
    REQUEST_RETRIES = 2

    def __init__(self, server_url: str) -> None:
        server_url = server_url.strip().rstrip("/")
        if not server_url.startswith(("http://", "https://")):
            server_url = "http://" + server_url
        self.server_url = server_url
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ImageMatcherPanel/1.0"})
        self.sid = ""

    def _url(self, api_path: str) -> str:
        return urljoin(self.server_url + "/", "webapi/" + api_path)

    def _timeout_message(self, action: str, read_timeout: int) -> str:
        return (
            f"Synology 云盘响应超时：{action} 时等待超过 {read_timeout} 秒。"
            "请确认这台电脑能打开 Synology Drive，或者稍后再试。"
        )

    def _connection_message(self, action: str) -> str:
        return (
            f"无法连接 Synology 云盘：{action} 失败。"
            "请确认服务器地址、公司网络/VPN、Synology 服务是否正常。"
        )

    def _get(
        self,
        api_path: str,
        action: str,
        *,
        read_timeout: int,
        retries: int | None = None,
        **kwargs,
    ) -> requests.Response:
        attempts = (self.REQUEST_RETRIES if retries is None else retries) + 1
        timeout = (self.CONNECT_TIMEOUT, read_timeout)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.get(self._url(api_path), timeout=timeout, **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(2 * attempt, 8))
                    continue
                raise RuntimeError(self._timeout_message(action, read_timeout)) from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(2 * attempt, 8))
                    continue
                raise RuntimeError(self._connection_message(action)) from exc
            except requests.exceptions.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                raise RuntimeError(
                    f"Synology 云盘返回异常：{action} 失败，HTTP 状态码 {status_code}。"
                ) from exc
        raise RuntimeError(str(last_error) if last_error else self._connection_message(action))

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
        response = self._get(
            "auth.cgi",
            "登录",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": username,
                "passwd": password,
                "session": "SynologyDrive",
                "format": "sid",
            },
            read_timeout=self.LOGIN_READ_TIMEOUT,
        )
        data = self._json(response, "logging in")
        if not data.get("success"):
            code = data.get("error", {}).get("code", "unknown")
            if code == 400:
                raise RuntimeError("Synology 登录失败：账号或密码不正确。")
            if code == 401:
                raise RuntimeError("Synology 登录失败：这个账号已被停用。")
            if code == 403:
                raise RuntimeError("Synology 登录失败：账号可能开启了二次验证。")
            raise RuntimeError(f"Synology 登录失败，错误代码：{code}")
        self.sid = data["data"]["sid"]

    def logout(self) -> None:
        if not self.sid:
            return
        self._get(
            "auth.cgi",
            "退出登录",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "logout",
                "session": "SynologyDrive",
                "_sid": self.sid,
            },
            read_timeout=10,
            retries=0,
        )
        self.sid = ""

    def _entry(self, params: dict, action: str) -> dict:
        params = dict(params)
        params["_sid"] = self.sid
        response = self._get("entry.cgi", action, params=params, read_timeout=self.LIST_READ_TIMEOUT)
        data = self._json(response, action)
        if not data.get("success"):
            code = data.get("error", {}).get("code", "unknown")
            if code == 408:
                raise RuntimeError("Synology 登录状态已过期，请重新搜索一次。")
            if code in {414, 105, 1000, 1002, 1003}:
                raise RuntimeError("没有找到这个 Synology 云盘目录，请检查云盘搜索目录。")
            if code in {407, 417}:
                raise RuntimeError("这个 Synology 账号没有权限读取该目录。")
            raise RuntimeError(f"Synology 请求失败：{action}，错误代码：{code}")
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
        response = self._get(
            "entry.cgi/" + (file_name or output_path.name),
            f"下载图片 {file_name}",
            params={
                "api": "SYNO.SynologyDrive.Files",
                "version": "2",
                "method": "download",
                "files": f'["id:{file_id}"]',
                "force_download": "true",
                "json_error": "true",
                "_sid": self.sid,
            },
            read_timeout=self.DOWNLOAD_READ_TIMEOUT,
        )
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            data = self._json(response, f"downloading Drive file {file_name}")
            code = data.get("error", {}).get("code", "unknown")
            raise RuntimeError(f"Synology Drive 图片下载失败，错误代码：{code}")
        if not response.content:
            raise RuntimeError(f"Synology Drive 下载 {file_name} 时返回了空文件。")
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
