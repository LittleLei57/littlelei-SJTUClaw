"""Bounded, read-only access to public GitHub repositories.

The service deliberately talks only to GitHub's public Contents API (or the
raw.githubusercontent.com endpoint when a raw URL is supplied).  It never
executes repository code and places hard limits on the number of files and
bytes returned to the agent.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import socket
import time
import zipfile
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen


GITHUB_API_HOST = "api.github.com"
GITHUB_HOST = "github.com"
GITHUB_RAW_HOST = "raw.githubusercontent.com"
GITHUB_CODELOAD_HOST = "codeload.github.com"
ALLOWED_HOSTS = frozenset({GITHUB_API_HOST, GITHUB_HOST, GITHUB_RAW_HOST, GITHUB_CODELOAD_HOST})
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES = 200_000
MAX_FILES = 50
MAX_BYTES = 500_000
MAX_FILE_BYTES = 100_000
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


class GitHubReadError(RuntimeError):
    """A safe, user-facing GitHub read error (without response/body details)."""

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retryable = retryable


class GitHubReader:
    """Read public text files from GitHub with strict bounds."""

    RETRY_ATTEMPTS = 3
    RETRY_BACKOFF = (0.5, 1.0)

    def __init__(
        self,
        timeout: float = 15.0,
        opener: Callable[..., Any] = urlopen,
        token: str | None = None,
    ) -> None:
        self.timeout = timeout
        self._opener = opener
        # A token is optional: public repositories remain usable without one,
        # while developers who already use gh/GitHub can raise the very small
        # anonymous Contents API rate limit.  It is sent only to api.github.com.
        self._token = (token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()

    def read(
        self,
        repo: str,
        path: str = "",
        ref: str = "main",
        max_files: int = DEFAULT_MAX_FILES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        recursive: bool = False,
    ) -> dict[str, Any]:
        owner, repository, parsed_ref, parsed_path, raw_url = _parse_target(repo, path, ref)
        ref = parsed_ref or "main"
        path = parsed_path
        max_files = _bounded_int(max_files, "max_files", 1, MAX_FILES)
        max_bytes = _bounded_int(max_bytes, "max_bytes", 1_000, MAX_BYTES)

        if raw_url:
            item = self._fetch_raw(raw_url, path or None, max_bytes)
            return {
                "repository": f"{owner}/{repository}",
                "ref": ref,
                "path": path,
                "files": [item],
                "fileCount": 1,
                "bytesRead": item["bytesRead"],
                "truncated": item["truncated"],
                "source": raw_url,
                "citations": [{"label": "[G1]", "kind": "github", "title": item["path"], "url": raw_url}],
            }

        files: list[dict[str, Any]] = []
        state = {"bytes": 0, "truncated": False}
        source_url = f"https://{GITHUB_API_HOST}/repos/{owner}/{repository}/contents"
        try:
            self._read_contents(
                owner, repository, ref, path, max_files, max_bytes, recursive,
                files, state,
            )
        except GitHubReadError as exc:
            # The unauthenticated Contents API has a low shared rate limit.
            # For a concrete file, raw.githubusercontent.com is both cheaper
            # and safer than downloading an entire repository archive.  This
            # is important for large repositories: the previous archive-first
            # fallback made the tool's own "specify path" advice ineffective.
            if not getattr(exc, "rate_limited", False) and "rate limit" not in str(exc).lower():
                raise
            files.clear()
            state = {"bytes": 0, "truncated": False}
            if path:
                raw_url = self._raw_file_url(owner, repository, ref, path)
                try:
                    item = self._fetch_raw(raw_url, path, max_bytes)
                except GitHubReadError as raw_exc:
                    # A raw 404 normally means the requested path is a
                    # directory (or the ref/path is wrong).  Archive fallback
                    # can still serve small directories, but a large archive
                    # now reports a precise, non-retryable next action.
                    if "404" not in str(raw_exc):
                        raise
                    self._read_archive(
                        owner, repository, ref, path, max_files, max_bytes, recursive,
                        files, state,
                    )
                    source_url = (
                        f"https://{GITHUB_CODELOAD_HOST}/{owner}/{repository}/zip/refs/heads/"
                        f"{quote(ref, safe='/')}"
                    )
                else:
                    files.append(item)
                    state["bytes"] = int(item.get("bytesRead") or 0)
                    state["truncated"] = bool(item.get("truncated"))
                    source_url = raw_url
            else:
                self._read_archive(
                    owner, repository, ref, path, max_files, max_bytes, recursive,
                    files, state,
                )
                source_url = (
                    f"https://{GITHUB_CODELOAD_HOST}/{owner}/{repository}/zip/refs/heads/"
                    f"{quote(ref, safe='/')}"
                )
        citations = []
        for index, item in enumerate(files, start=1):
            if item.get("type") != "file":
                continue
            item_url = (
                f"https://{GITHUB_HOST}/{quote(owner)}/{quote(repository)}/blob/"
                f"{quote(ref, safe='/')}/{quote(str(item.get('path') or ''), safe='/')}"
            )
            item["url"] = item_url
            citations.append({
                "label": f"[G{len(citations) + 1}]",
                "kind": "github",
                "title": item.get("path"),
                "url": item_url,
            })
        return {
            "repository": f"{owner}/{repository}",
            "ref": ref,
            "path": path or ".",
            "files": files,
            "fileCount": len(files),
            "bytesRead": state["bytes"],
            "truncated": bool(state["truncated"]),
            "source": source_url,
            "citations": citations,
        }

    # Keep the handler name discoverable for integrations that instantiate the
    # service directly instead of going through ``bootstrap.build_runtime``.
    github_read = read

    @staticmethod
    def _raw_file_url(owner: str, repository: str, ref: str, path: str) -> str:
        """Build an allowlisted raw URL for a known repository file."""
        return (
            f"https://{GITHUB_RAW_HOST}/{quote(owner)}/{quote(repository)}/"
            f"{quote(ref, safe='/')}/{quote(path, safe='/')}"
        )

    def _read_contents(
        self,
        owner: str,
        repository: str,
        ref: str,
        path: str,
        max_files: int,
        max_bytes: int,
        recursive: bool,
        files: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> None:
        if len(files) >= max_files or state["bytes"] >= max_bytes:
            state["truncated"] = True
            return
        payload = self._request_json(
            f"https://{GITHUB_API_HOST}/repos/{quote(owner)}/{quote(repository)}/contents/"
            f"{quote(path, safe='/')}?ref={quote(ref, safe='')}",
        )
        if isinstance(payload, dict):
            item_type = payload.get("type")
            item_path = _safe_repo_path(str(payload.get("path") or path))
            if item_type == "dir":
                # The Contents API returns a list for directories; a dict here
                # is unexpected and is treated as malformed rather than guessed.
                raise GitHubReadError("GitHub 返回了异常的目录内容。")
            if item_type != "file":
                raise GitHubReadError("GitHub 目标不是可读取的文本文件。")
            self._append_file(payload, item_path, max_bytes, files, state)
            return
        if not isinstance(payload, list):
            raise GitHubReadError("GitHub 返回了异常的内容格式。")
        for item in payload:
            if len(files) >= max_files or state["bytes"] >= max_bytes:
                state["truncated"] = True
                break
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            item_path = _safe_repo_path(str(item.get("path") or ""))
            if item_type == "file":
                self._append_file(item, item_path, max_bytes, files, state)
            elif item_type == "dir" and recursive:
                self._read_contents(
                    owner, repository, ref, item_path, max_files, max_bytes,
                    recursive, files, state,
                )
            else:
                # Directory entries are useful to the caller even when we do
                # not recurse, without fetching arbitrary repository content.
                if item_type == "dir":
                    files.append({"path": item_path, "type": "directory"})
        if len(files) > max_files:
            del files[max_files:]
            state["truncated"] = True

    def _append_file(
        self,
        item: dict[str, Any],
        item_path: str,
        max_bytes: int,
        files: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> None:
        content = _decode_contents_payload(item.get("content"))
        if content is None:
            download_url = item.get("download_url")
            if not isinstance(download_url, str) or not _is_allowed_url(download_url, GITHUB_RAW_HOST):
                raise GitHubReadError("GitHub 文件没有可读取的公开内容。")
            raw = self._fetch_raw(download_url, None, min(MAX_FILE_BYTES, max_bytes - state["bytes"]))
            content = raw.get("content", "")
            file_truncated = bool(raw.get("truncated"))
            bytes_read = int(raw.get("bytesRead") or len(content.encode("utf-8")))
        else:
            file_truncated = False
            bytes_read = len(content.encode("utf-8"))
        remaining = max(0, max_bytes - state["bytes"])
        encoded = content.encode("utf-8")
        if len(encoded) > remaining:
            content = encoded[:remaining].decode("utf-8", errors="replace")
            bytes_read = len(content.encode("utf-8"))
            file_truncated = True
            state["truncated"] = True
        if len(encoded) > MAX_FILE_BYTES:
            content = encoded[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
            bytes_read = len(content.encode("utf-8"))
            file_truncated = True
            state["truncated"] = True
        state["bytes"] += bytes_read
        files.append({
            "path": item_path,
            "type": "file",
            "untrusted": True,
            "size": item.get("size"),
            "content": content,
            "bytesRead": bytes_read,
            "truncated": file_truncated,
        })

    def _read_archive(
        self,
        owner: str,
        repository: str,
        ref: str,
        path: str,
        max_files: int,
        max_bytes: int,
        recursive: bool,
        files: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> None:
        """Read a public repository from a bounded codeload ZIP fallback."""
        archive_url = (
            f"https://{GITHUB_CODELOAD_HOST}/{quote(owner)}/{quote(repository)}"
            f"/zip/refs/heads/{quote(ref, safe='/')}"
        )
        archive = self._request_archive_bytes(archive_url)
        try:
            zf = zipfile.ZipFile(io.BytesIO(archive))
        except (zipfile.BadZipFile, OSError) as exc:
            raise GitHubReadError("GitHub 备用归档不是有效的 ZIP 文件。") from exc

        wanted = path.strip("/")
        entries: list[tuple[str, zipfile.ZipInfo]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:  # untrusted symlink
                continue
            raw_name = str(info.filename or "").replace("\\", "/")
            parts = [part for part in raw_name.split("/") if part]
            if not parts or any(part in {".", ".."} for part in parts):
                continue
            # codeload wraps every member in one generated directory.
            member = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
            if not member or "\x00" in member:
                continue
            if wanted and not (member == wanted or member.startswith(wanted + "/")):
                continue
            if wanted and not recursive and member != wanted:
                continue
            entries.append((member, info))

        entries.sort(key=lambda item: item[0].lower())
        for member, info in entries:
            if len(files) >= max_files or state["bytes"] >= max_bytes:
                state["truncated"] = True
                break
            try:
                read_limit = min(MAX_FILE_BYTES, max_bytes - state["bytes"]) + 1
                # ZipFile.read() has no size argument; use a bounded stream
                # read so a compressed bomb cannot expand without a limit.
                with zf.open(info, "r") as member_file:
                    raw = member_file.read(max(1, read_limit))
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise GitHubReadError("GitHub 备用归档读取失败。") from exc
            if b"\x00" in raw[:4096]:  # binary file; skip rather than expose bytes
                continue
            remaining = max(0, max_bytes - state["bytes"])
            limited = raw[: min(MAX_FILE_BYTES, remaining)]
            content = limited.decode("utf-8", errors="replace")
            truncated = len(raw) > len(limited) or info.file_size > len(raw)
            if truncated:
                state["truncated"] = True
            bytes_read = len(limited)
            state["bytes"] += bytes_read
            files.append({
                "path": member,
                "type": "file",
                "untrusted": True,
                "size": info.file_size,
                "content": content,
                "bytesRead": bytes_read,
                "truncated": truncated,
            })

    def _request_archive_bytes(self, url: str) -> bytes:
        for attempt in range(self.RETRY_ATTEMPTS):
            try:
                return self._request_archive_bytes_once(url)
            except GitHubReadError as exc:
                if not exc.retryable or attempt + 1 >= self.RETRY_ATTEMPTS:
                    raise
                time.sleep(self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)])
        raise GitHubReadError("GitHub 归档下载失败，请稍后重试。")

    def _request_archive_bytes_once(self, url: str) -> bytes:
        if not _is_allowed_url(url, GITHUB_CODELOAD_HOST):
            raise GitHubReadError("只允许访问 GitHub 官方归档域名。")
        request = Request(url, headers={"User-Agent": "SJTUClaw/1.0"})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                final_url = getattr(response, "geturl", lambda: url)()
                if not isinstance(final_url, str) or not _is_allowed_url(final_url, GITHUB_CODELOAD_HOST):
                    raise GitHubReadError("GitHub 响应跳转到了不受信任的域名。")
                raw = response.read(MAX_ARCHIVE_BYTES + 1)
                if len(raw) > MAX_ARCHIVE_BYTES:
                    raise GitHubReadError(
                        "GitHub 仓库归档本身超过 20 MB 安全上限。"
                        "不要继续重试整仓库或目录；请改为指定具体文件 path"
                        "（例如 README.md、docs/guide.md），具体文件会绕过整仓下载。"
                    )
                return raw
        except GitHubReadError:
            raise
        except HTTPError as exc:
            if exc.code == 404:
                raise GitHubReadError("GitHub 仓库、分支或路径不存在（HTTP 404）。") from exc
            raise GitHubReadError(
                f"GitHub 备用归档请求失败（HTTP {exc.code}）。",
                retryable=exc.code == 429 or exc.code >= 500,
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise GitHubReadError(
                "GitHub 备用归档连接失败或请求超时，请稍后重试。",
                retryable=True,
            ) from exc

    def _fetch_raw(self, url: str, path: str | None, max_bytes: int) -> dict[str, Any]:
        if not _is_allowed_url(url, GITHUB_RAW_HOST):
            raise GitHubReadError("只允许读取 github.com 的公开仓库内容。")
        raw = self._request_bytes(url)
        if b"\x00" in raw[:4096]:
            raise GitHubReadError("GitHub 目标文件是二进制文件，当前只支持读取文本内容。")
        limited = raw[: min(max_bytes, MAX_FILE_BYTES) + 1]
        truncated = len(limited) > min(max_bytes, MAX_FILE_BYTES)
        content = limited[: min(max_bytes, MAX_FILE_BYTES)].decode("utf-8", errors="replace")
        return {
            "path": path or urlsplit(url).path.rsplit("/", 1)[-1],
            "type": "file",
            "untrusted": True,
            "content": content,
            "bytesRead": len(content.encode("utf-8")),
            "truncated": truncated,
        }

    def _request_json(self, url: str) -> Any:
        if not _is_allowed_url(url, GITHUB_API_HOST):
            raise GitHubReadError("只允许访问 GitHub 官方 API。")
        raw = self._request_bytes(url, accept="application/vnd.github+json")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReadError("GitHub 返回了无法解析的响应。") from exc

    def _request_bytes(self, url: str, accept: str = "text/plain") -> bytes:
        for attempt in range(self.RETRY_ATTEMPTS):
            try:
                return self._request_bytes_once(url, accept)
            except GitHubReadError as exc:
                if not exc.retryable or attempt + 1 >= self.RETRY_ATTEMPTS:
                    raise
                time.sleep(self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)])
        raise GitHubReadError("GitHub 请求失败，请稍后重试。")

    def _request_bytes_once(self, url: str, accept: str = "text/plain") -> bytes:
        headers = {"Accept": accept, "User-Agent": "SJTUClaw/1.0"}
        if self._token and (urlsplit(url).hostname or "").lower() == GITHUB_API_HOST:
            headers["Authorization"] = f"Bearer {self._token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        request = Request(url, headers=headers)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                final_url = getattr(response, "geturl", lambda: url)()
                if not isinstance(final_url, str) or not _is_allowed_url(final_url):
                    raise GitHubReadError("GitHub 响应跳转到了不受信任的域名。")
                return response.read(MAX_BYTES + 1)
        except GitHubReadError:
            raise
        except HTTPError as exc:
            if exc.code == 404:
                raise GitHubReadError("GitHub 仓库、分支或路径不存在（HTTP 404）。") from exc
            if exc.code in {401, 403}:
                raise GitHubReadError(
                    "GitHub 拒绝了请求，可能触发了公开 API 限流。",
                    rate_limited=True,
                ) from exc
            raise GitHubReadError(
                f"GitHub 请求失败（HTTP {exc.code}）。",
                retryable=exc.code == 429 or exc.code >= 500,
            ) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise GitHubReadError(
                "GitHub 网络连接失败或请求超时，请稍后重试。",
                retryable=True,
            ) from exc


def _parse_target(repo: str, path: str, ref: str) -> tuple[str, str, str, str, str | None]:
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo 不能为空，应为 owner/name 或 GitHub URL。")
    repo = repo.strip()
    path = _safe_repo_path(path or "")
    ref = _safe_ref(ref or "main")
    if "://" in repo:
        parsed = urlsplit(repo)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("只允许使用 https 的 github.com 公开仓库 URL。")
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if parsed.hostname == GITHUB_RAW_HOST:
            if len(parts) < 4:
                raise ValueError("raw GitHub URL 必须包含 owner、repo、ref 和文件路径。")
            owner, repository, url_ref = parts[:3]
            raw_path = "/".join(parts[3:])
            _validate_repo(owner, repository)
            return owner, repository, _safe_ref(url_ref), _safe_repo_path(raw_path), repo
        if len(parts) < 2:
            raise ValueError("GitHub URL 必须包含 owner/name。")
        owner, repository = parts[:2]
        _validate_repo(owner, repository)
        if len(parts) >= 4 and parts[2] in {"blob", "tree"}:
            url_ref = parts[3]
            url_path = "/".join(parts[4:])
            return owner, repository, _safe_ref(url_ref), _safe_repo_path(url_path), None
        if len(parts) >= 5 and parts[2] == "raw":
            # GitHub also serves raw files from github.com/.../raw/... in
            # addition to raw.githubusercontent.com/... .  Normalize that
            # form to the dedicated raw host before fetching it.
            if len(parts) >= 7 and parts[3] == "refs" and parts[4] in {"heads", "tags"}:
                url_ref, url_path = parts[5], "/".join(parts[6:])
            else:
                url_ref, url_path = parts[3], "/".join(parts[4:])
            safe_ref = _safe_ref(url_ref)
            safe_path = _safe_repo_path(url_path)
            raw_url = (
                f"https://{GITHUB_RAW_HOST}/{quote(owner)}/{quote(repository)}/"
                f"{quote(safe_ref, safe='/')}/{quote(safe_path, safe='/')}"
            )
            return owner, repository, safe_ref, safe_path, raw_url
        return owner, repository, ref, path, None
    compact = repo.strip("/")
    bits = compact.split("/", 1)
    if len(bits) != 2:
        raise ValueError("repo 应为 owner/name。")
    owner, repository = bits
    _validate_repo(owner, repository)
    return owner, repository, ref, path, None


def _validate_repo(owner: str, repository: str) -> None:
    if (
        owner in {".", ".."}
        or repository in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
    ):
        raise ValueError("GitHub owner/name 格式无效。")


def _safe_repo_path(path: str) -> str:
    path = unquote(str(path or "")).replace("\\", "/").strip("/")
    if "\x00" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        if path:
            raise ValueError("GitHub 路径包含不安全的片段。")
        return ""
    if len(path) > 500:
        raise ValueError("GitHub 路径过长。")
    return str(PurePosixPath(path)) if path else ""


def _safe_ref(ref: str) -> str:
    ref = unquote(str(ref or "")).strip()
    if not ref or "\x00" in ref or ".." in ref or len(ref) > 200 or any(ch in ref for ch in "?#"):
        raise ValueError("GitHub ref 格式无效。")
    return ref


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low} 到 {high} 之间。")
    return value


def _is_allowed_url(url: str, expected_host: str | None = None) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        return False
    return expected_host is None or host == expected_host


def _decode_contents_payload(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


GitHubService = GitHubReader

__all__ = ["GitHubReader", "GitHubService", "GitHubReadError"]
