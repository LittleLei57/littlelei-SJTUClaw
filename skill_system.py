"""Step 9：本地 Skill Registry、渐进加载、安装与使用记录。

``SkillRegistry`` 只扫描各目录的 ``SKILL.md`` frontmatter，先向模型提供轻量索引；
用户或模型明确选择后，``SkillService`` 才把完整说明设为当前 Session 的
Active Skill，并允许按需读取同目录资源。这种渐进披露避免所有 Skill 挤占上下文。

Skill 可以指导 Agent 组合已有 Tools，也可以在受限目录中携带脚本和模板。
使用/安装记录会关联 Session；安装、脚本执行和其他有副作用的动作仍经过 Approval，
不能用 Skill 绕过 Workspace、超时和 Tool 安全边界。
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import importlib.util
from pathlib import Path
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile
import sys

from session_store import Session, SessionStore, utc_now
from tools import Tool, ToolExecutionContext, ToolRegistry


FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillDownloadHTTPError(ValueError):
    """An HTTP failure from a Skill registry/download endpoint.

    Keeping the status and a short server-provided detail lets the ClawHub
    adapter retry a deterministic version without weakening the generic
    download validation path.
    """

    def __init__(self, status_code: int, url: str, detail: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.detail = detail.strip()[:800]
        self.retryable = status_code == 429 or status_code >= 500
        suffix = f"；服务端说明：{self.detail}" if self.detail else ""
        super().__init__(f"Skill 下载失败：HTTP {status_code}{suffix}；下载地址：{url}")


@dataclass(frozen=True)
class Skill:
    """从一个 SKILL.md 解析出的名称、说明、路径与资源元数据。"""

    name: str
    description: str
    directory: Path
    skill_file: Path
    metadata: dict[str, str]

    def basic_info(self, health: dict | None = None) -> dict:
        resources = [
            str(path.relative_to(self.directory)).replace("\\", "/")
            for folder in ("references", "scripts", "assets")
            if (self.directory / folder).exists()
            for path in sorted((self.directory / folder).rglob("*"))
            if path.is_file()
        ]
        result = {
            "name": self.name,
            "description": self.description,
            "resources": resources,
        }
        if health is not None:
            result["health"] = health
        return result


class SkillRegistry:
    """扫描 Skill 根目录并维护可检索的轻量索引。"""

    # A Skill can legitimately bundle long reference material and HTML
    # templates.  The old 200k aggregate cap made otherwise valid skills
    # (notably guizang-ppt-skill) impossible to activate.  Keep a bounded,
    # configurable prompt budget instead of rejecting the whole Skill.
    # Only SKILL.md is placed in the prompt eagerly.  Bundled references,
    # scripts and assets are disclosed as a manifest and fetched on demand.
    # This keeps a large Skill from consuming the whole conversation budget.
    DEFAULT_MAX_RESOURCE_CHARS = 500_000
    MAX_RESOURCE_CHARS = DEFAULT_MAX_RESOURCE_CHARS
    MAX_RESOURCE_MANIFEST_ENTRIES = 160

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self.max_resource_chars = self._resource_budget()
        self._skills: dict[str, Skill] = {}
        self.scan()

    @classmethod
    def _resource_budget(cls) -> int:
        """Return the active Skill prompt budget.

        The environment override is intentionally an upper-bounded escape
        hatch for local users with large Skills.  It changes how much text is
        placed in the model prompt; it does not disable archive/path/symlink
        validation or execute files from a Skill package.
        """
        raw = os.getenv("SJTUCLAW_MAX_SKILL_RESOURCE_CHARS", "")
        if not raw.strip():
            return cls.DEFAULT_MAX_RESOURCE_CHARS
        try:
            value = int(raw)
        except ValueError:
            return cls.DEFAULT_MAX_RESOURCE_CHARS
        return max(64_000, min(value, 2_000_000))

    def scan(self) -> list[Skill]:
        self._skills.clear()
        if not self.skills_dir.exists():
            return []
        for directory in sorted(path for path in self.skills_dir.iterdir() if path.is_dir()):
            skill_file = directory / "SKILL.md"
            if not skill_file.exists():
                continue
            text = skill_file.read_text(encoding="utf-8")
            metadata = self._parse_frontmatter(text, skill_file)
            name = metadata.get("name", "")
            description = metadata.get("description", "")
            if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
                raise ValueError(f"Skill name 非法：{skill_file}")
            if name != directory.name and directory.name != f"{name}-skill":
                raise ValueError(f"Skill name 必须与目录名一致：{skill_file}")
            if not description:
                raise ValueError(f"Skill description 不能为空：{skill_file}")
            if name in self._skills:
                raise ValueError(f"Skill name 重复：{name}")
            self._skills[name] = Skill(
                name, description, directory, skill_file, metadata
            )
        return self.list()

    @staticmethod
    def _parse_frontmatter(text: str, source: Path) -> dict[str, str]:
        match = FRONTMATTER.match(text)
        if not match:
            raise ValueError(f"SKILL.md 缺少 YAML frontmatter：{source}")
        metadata = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("\"'")
        return metadata

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def index(self) -> list[dict[str, str]]:
        result = []
        for skill in self.list():
            health = self.diagnose(skill.name)
            result.append({
                "name": skill.name,
                "description": skill.description,
                "availability": health["status"],
            })
        return result

    def get(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"Skill 不存在：{name}")
        return skill

    @staticmethod
    def _metadata_items(value: str | None) -> list[str]:
        """Parse a compact frontmatter list without requiring PyYAML.

        Skill metadata intentionally stays compatible with the project's
        existing minimal frontmatter reader. Both comma-separated values and
        JSON-like ``[a, b]`` lists are accepted.
        """
        text = str(value or "").strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [
            item.strip().strip("\"'")
            for item in text.split(",")
            if item.strip().strip("\"'")
        ]

    @staticmethod
    def _platform_name() -> str:
        if sys.platform.startswith("win"):
            return "windows"
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        return sys.platform.lower()

    def diagnose(self, name: str) -> dict:
        """Passively inspect whether a Skill is ready for the current host.

        The doctor never imports third-party modules or runs bundled commands.
        It only checks declarations and filesystem/runtime availability, so
        listing Skills cannot execute untrusted package code.
        """
        skill = self.get(name)
        metadata = skill.metadata
        checks: list[dict[str, str]] = []
        blocking = False
        needs_configuration = False

        def add(key: str, label: str, status: str, detail: str) -> None:
            nonlocal blocking, needs_configuration
            checks.append({
                "key": key,
                "label": label,
                "status": status,
                "detail": detail,
            })
            if status == "failed":
                blocking = True
            elif status == "configuration":
                needs_configuration = True

        add("manifest", "Skill 清单", "passed", "SKILL.md 与 frontmatter 可读取。")

        declared_platforms = {
            item.lower()
            for item in self._metadata_items(metadata.get("platforms"))
        }
        current_platform = self._platform_name()
        if declared_platforms:
            supported = current_platform in declared_platforms
            add(
                "platform",
                "当前平台",
                "passed" if supported else "failed",
                (
                    f"当前为 {current_platform}。"
                    if supported
                    else f"仅支持 {', '.join(sorted(declared_platforms))}，当前为 {current_platform}。"
                ),
            )

        for command in self._metadata_items(metadata.get("requires-commands")):
            path = shutil.which(command)
            add(
                f"command:{command}",
                f"命令 {command}",
                "passed" if path else "failed",
                path or "当前 PATH 中未找到。",
            )

        for package in self._metadata_items(metadata.get("requires-python")):
            try:
                available = importlib.util.find_spec(package) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                available = False
            add(
                f"python:{package}",
                f"Python 包 {package}",
                "passed" if available else "failed",
                "已安装。" if available else "当前 Python 环境中未找到。",
            )

        for package in self._metadata_items(metadata.get("requires-node")):
            package_path = Path(*package.split("/"))
            available = any(
                (parent / "node_modules" / package_path / "package.json").is_file()
                for parent in (skill.directory, *skill.directory.parents)
            )
            add(
                f"node:{package}",
                f"Node 包 {package}",
                "passed" if available else "failed",
                "已安装。" if available else "当前项目的 node_modules 中未找到。",
            )

        for variable in self._metadata_items(metadata.get("requires-env")):
            available = bool(os.getenv(variable, "").strip())
            add(
                f"env:{variable}",
                f"配置 {variable}",
                "passed" if available else "configuration",
                "已配置。" if available else "尚未配置环境变量。",
            )

        text = skill.skill_file.read_text(encoding="utf-8")
        missing_resources: list[str] = []
        for raw_target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            target = target.split("#", 1)[0].split("?", 1)[0]
            if (
                not target
                or target.startswith(("#", "/", "\\"))
                or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE)
            ):
                continue
            candidate = (skill.directory / target).resolve()
            try:
                candidate.relative_to(skill.directory.resolve())
            except ValueError:
                missing_resources.append(target)
                continue
            if not candidate.is_file():
                missing_resources.append(target)
        if missing_resources:
            add(
                "resources",
                "引用资源",
                "warning",
                "未找到：" + "、".join(sorted(set(missing_resources))[:5]),
            )
        else:
            add("resources", "引用资源", "passed", "显式引用的本地资源均可用。")

        runtime_kind = metadata.get("runtime", "").strip().lower()
        has_requirements = any(
            self._metadata_items(metadata.get(key))
            for key in (
                "platforms",
                "requires-commands",
                "requires-python",
                "requires-node",
                "requires-env",
            )
        )
        if blocking:
            status = "unavailable"
            summary = "缺少运行依赖，暂不可用"
        elif needs_configuration:
            status = "needs_configuration"
            summary = "需要补充配置后使用"
        elif runtime_kind == "prompt-only" or has_requirements:
            status = "ready"
            summary = "已通过被动检查"
        else:
            status = "unverified"
            summary = "未声明运行依赖，尚未验证"
        return {
            "status": status,
            "summary": summary,
            "checkedAt": utc_now(),
            "runtime": runtime_kind or "unspecified",
            "checks": checks,
        }

    def load(self, name: str) -> str:
        skill = self.get(name)
        sections = [f"## SKILL.md\n{skill.skill_file.read_text(encoding='utf-8')}"]
        resources: list[tuple[str, int, bool]] = []
        for folder in ("references", "scripts", "assets"):
            root = skill.directory / folder
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = str(path.relative_to(skill.directory)).replace("\\", "/")
                try:
                    size = len(path.read_text(encoding="utf-8"))
                except UnicodeDecodeError:
                    size = path.stat().st_size
                    resources.append((relative, size, False))
                else:
                    resources.append((relative, size, True))
        if resources:
            resources.sort(key=lambda item: (item[0].count("/"), item[0]))
            shown = resources[:self.MAX_RESOURCE_MANIFEST_ENTRIES]
            omitted = len(resources) - len(shown)
            lines = [
                "## Resource manifest",
                (
                    "Bundled resources are not embedded in the prompt. "
                    "Use read_skill_resource with a listed relative path and "
                    "read only the chunks needed for the current step."
                ),
            ]
            lines.extend(
                f"- {path} ({size} {'chars' if readable else 'bytes; binary'})"
                for path, size, readable in shown
            )
            if omitted:
                lines.append(
                    f"- … {omitted} additional deeply nested resources omitted "
                    "from the prompt manifest; inspect a relevant parent script "
                    "before requesting one by exact relative path."
                )
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


class SkillService:
    """负责激活、读取资源、执行和安装 Skill 的 Session 级服务。"""

    MAX_RESOURCE_CHUNK_CHARS = 40_000
    # Downloads are deliberately bounded.  Skills are instructions/resources,
    # not arbitrary application bundles, so a very large archive is almost
    # certainly a mistake (or a zip-bomb).
    MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
    MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
    MAX_FILES = 256
    MAX_FILE_BYTES = 5 * 1024 * 1024
    # Registry metadata and GitHub handoffs can involve several bounded HTTP
    # requests.  Keep each request finite, but allow normal CI/student-network
    # latency instead of failing most installs at 25 seconds.
    DOWNLOAD_TIMEOUT = 60
    # Retry only transient registry failures. Permanent source/validation
    # errors (404, 409, oversized archives, malformed packages) stay terminal.
    DOWNLOAD_RETRY_ATTEMPTS = 3
    DOWNLOAD_RETRY_BACKOFF = (0.5, 1.0)
    SCRIPT_SUFFIXES = {
        ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash",
        ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".com", ".exe", ".dll",
        ".so", ".dylib", ".bin",
    }

    def __init__(self, registry: SkillRegistry, sessions: SessionStore):
        self.registry = registry
        self.sessions = sessions
        self._refresh_callbacks: list[Any] = []

    def add_refresh_callback(self, callback) -> None:
        """Register a callback used to refresh ContextBuilder's skill index."""
        if callback is not None and callback not in self._refresh_callbacks:
            self._refresh_callbacks.append(callback)

    def install(
        self,
        context: ToolExecutionContext,
        source: str,
        version: str = "latest",
        allow_scripts: bool = False,
    ) -> dict:
        """Download and atomically install one verified Skill.

        ``source`` may be a GitHub repository/archive URL or ``clawhub:<slug>``.
        This handler never executes files from the archive.  Installation is
        approval-gated by the Tool registration below.
        """
        del context  # Installation is global, but approval is still per turn.
        version = self._validate_version(version)
        archive, source_kind, resolved_source = self._download_source(source, version)
        installed_name = self._install_archive(
            archive,
            allow_scripts=allow_scripts,
        )
        # Registry.scan validates the complete installed tree and makes the
        # Skill immediately visible to subsequent turns.
        try:
            self.registry.scan()
        except Exception:
            target = self.registry.skills_dir / installed_name
            shutil.rmtree(target, ignore_errors=True)
            self.registry.scan()
            raise
        for callback in list(self._refresh_callbacks):
            callback()
        skill = self.registry.get(installed_name)
        # Keep a small provenance record, mirroring OpenClaw's .clawhub
        # origin metadata.  It makes later updates/debugging deterministic
        # without executing anything from the downloaded archive.
        origin_dir = skill.directory / ".clawhub"
        origin_dir.mkdir(exist_ok=True)
        (origin_dir / "origin.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "sourceKind": source_kind,
                    "resolvedSource": resolved_source,
                    "containsApprovedScripts": bool(allow_scripts),
                    "installedAt": utc_now(),
                    "version": version,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return {
            "success": True,
            "skillName": skill.name,
            "description": skill.description,
            "source": source_kind,
            "resolvedSource": resolved_source,
            "message": f"Skill {skill.name} 已安全安装并刷新索引。",
        }

    @classmethod
    def _validate_version(cls, version: str) -> str:
        if not isinstance(version, str) or not version.strip():
            return "latest"
        value = version.strip()
        if len(value) > 128 or any(char in value for char in "\\/\x00"):
            raise ValueError("Skill version/tag 非法。")
        return value

    @classmethod
    def _validate_http_url(cls, url: str, allowed_hosts: set[str]) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Skill 下载仅允许 HTTPS URL。")
        host = parsed.hostname.lower().rstrip(".")
        if host not in allowed_hosts:
            raise ValueError(f"不允许从该域名下载 Skill：{host}")
        if parsed.username or parsed.password:
            raise ValueError("Skill URL 不得包含账号或密码。")
        return urlunparse(("https", host, parsed.path, parsed.params, parsed.query, ""))

    @classmethod
    def _github_url(cls, source: str, version: str) -> str:
        parsed = urlparse(source)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = {"github.com", "www.github.com", "codeload.github.com"}
        if host not in allowed:
            raise ValueError("不是受支持的 GitHub URL。")
        parts = [part for part in parsed.path.split("/") if part]
        if host == "codeload.github.com":
            if len(parts) < 4 or parts[2] != "zip":
                raise ValueError("GitHub archive URL 格式无效。")
            return cls._validate_http_url(source, {"codeload.github.com"})
        if len(parts) < 2:
            raise ValueError("GitHub URL 必须包含 owner/repository。")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise ValueError("GitHub owner/repository 名称无效。")
        # Accept an explicit archive URL, otherwise pin the requested ref.
        if len(parts) >= 3 and parts[2] == "archive":
            return cls._validate_http_url(source, {"github.com", "www.github.com"})
        ref = quote(version if version != "latest" else "main", safe="A-Za-z0-9._-~")
        return f"https://codeload.github.com/{quote(owner)}/{quote(repo)}/zip/{ref}"

    @classmethod
    def _download_bytes(cls, url: str, allowed_hosts: set[str]) -> tuple[bytes, str, str]:
        """Download a bounded response with short transient-error retries."""
        for attempt in range(cls.DOWNLOAD_RETRY_ATTEMPTS):
            try:
                return cls._download_bytes_once(url, allowed_hosts)
            except SkillDownloadHTTPError as exc:
                if not exc.retryable or attempt + 1 >= cls.DOWNLOAD_RETRY_ATTEMPTS:
                    raise
                time.sleep(cls.DOWNLOAD_RETRY_BACKOFF[min(attempt, len(cls.DOWNLOAD_RETRY_BACKOFF) - 1)])
        raise ValueError("Skill download failed after bounded retries")

    @classmethod
    def _download_bytes_once(cls, url: str, allowed_hosts: set[str]) -> tuple[bytes, str, str]:
        safe_url = cls._validate_http_url(url, allowed_hosts)

        class _CheckedRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                cls._validate_http_url(newurl, allowed_hosts)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        request = Request(safe_url, headers={"User-Agent": "SJTUClaw-SkillInstaller/1.0", "Accept": "application/zip, application/json"})
        try:
            with build_opener(_CheckedRedirect()).open(request, timeout=cls.DOWNLOAD_TIMEOUT) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > cls.MAX_ARCHIVE_BYTES:
                    raise ValueError("Skill 压缩包超过大小限制。")
                chunks: list[bytes] = []
                total = 0
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > cls.MAX_ARCHIVE_BYTES:
                        raise ValueError("Skill 压缩包超过大小限制。")
                    chunks.append(block)
                return b"".join(chunks), response.headers.get("Content-Type", ""), response.geturl()
        except HTTPError as exc:
            try:
                raw_detail = exc.read(4096).decode("utf-8", "replace")
            except Exception:
                raw_detail = ""
            detail = raw_detail.strip()
            if detail.startswith(("{", "[")):
                try:
                    payload = json.loads(detail)
                    if isinstance(payload, dict):
                        detail = next(
                            (str(payload.get(key)) for key in ("detail", "message", "error", "reason")
                             if payload.get(key)),
                            detail,
                        )
                except (TypeError, ValueError):
                    pass
            raise SkillDownloadHTTPError(exc.code, safe_url, detail) from exc
        except (URLError, TimeoutError) as exc:
            raise ValueError(f"Skill 下载失败：{exc}") from exc

    @staticmethod
    def _split_clawhub_slug(value: str) -> tuple[str | None, str]:
        """Accept both ``clawhub:slug`` and ``clawhub:owner/slug``."""
        parts = [part for part in value.split("/") if part]
        if len(parts) == 2:
            owner, slug = parts
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", owner):
                return owner, slug
        return None, value.strip("/")

    @classmethod
    def _download_source(cls, source: str, version: str) -> tuple[bytes, str, str]:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("必须提供 GitHub URL 或 clawhub:<slug>。")
        value = source.strip()
        # Models and users often use the compact ``github:owner/repo`` form.
        # Normalize it to the canonical HTTPS URL before applying the same
        # host/path validation as every other GitHub source.
        if value.lower().startswith("github:") or value.lower().startswith("github://"):
            repository = value.split(":", 1)[1].lstrip("/").strip()
            if repository:
                value = "https://github.com/" + repository
        if value.lower().startswith("clawhub:") or value.lower().startswith("clawhub://"):
            slug = value.split(":", 1)[1].lstrip("/").strip()
            owner_handle, slug = cls._split_clawhub_slug(slug)
            return cls._download_clawhub(slug, version, owner_handle=owner_handle)
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in {"clawhub.ai", "www.clawhub.ai"}:
            parts = [part for part in parsed.path.split("/") if part]
            # ClawHub's browser URLs are /<owner>/skills/<slug>, while the
            # download API addresses the globally unique skill slug.
            if len(parts) == 3 and parts[1].lower() == "skills":
                owner_handle = parts[0]
                slug = parts[2]
            else:
                owner_handle = None
                slug = "/".join(parts)
            if not slug or slug.count("/") > 1:
                raise ValueError("ClawHub Skill slug 无效。")
            return cls._download_clawhub(slug, version, owner_handle=owner_handle)
        if host in {"github.com", "www.github.com", "codeload.github.com"}:
            parts = [part for part in parsed.path.split("/") if part]
            # A repository root can contain many unrelated skills (for
            # example anthropics/skills).  Accept a /tree/<ref>/<subdir>
            # target and package only that directory instead of downloading
            # the whole repository and tripping the global file-count guard.
            if host in {"github.com", "www.github.com"} and len(parts) >= 5 and parts[2] == "tree":
                owner, repo = parts[0], parts[1].removesuffix(".git")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
                    raise ValueError("GitHub owner/repository 名称无效。")
                return cls._download_github_subdir(
                    owner, repo, parts[3], "/".join(parts[4:]),
                )
            url = cls._github_url(value, version)
            blob, _, resolved = cls._download_bytes(url, {"github.com", "www.github.com", "codeload.github.com"})
            return blob, "github", resolved
        raise ValueError("仅支持 GitHub 仓库/压缩包或 ClawHub Skill。")

    @classmethod
    def _download_github_subdir(
        cls, owner: str, repository: str, ref: str, subpath: str,
    ) -> tuple[bytes, str, str]:
        """Build a bounded ZIP from one GitHub directory via Contents API."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub owner/repository 名称无效。")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ".." in ref:
            raise ValueError("GitHub ref 格式无效。")
        clean_path = "/".join(part for part in subpath.split("/") if part)
        if not clean_path or any(part in {".", ".."} for part in clean_path.split("/")):
            raise ValueError("GitHub Skill 子目录路径无效。")

        files: list[tuple[str, bytes]] = []
        total = 0

        def api_json(path: str):
            url = (
                f"https://api.github.com/repos/{quote(owner)}/{quote(repository)}"
                f"/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
            )
            blob, _, _ = cls._download_bytes(url, {"api.github.com"})
            try:
                return json.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("GitHub Contents API 返回格式无效。") from exc

        def walk(path: str, relative: str = "") -> None:
            nonlocal total
            payload = api_json(path)
            if isinstance(payload, dict) and payload.get("type") == "file":
                size = payload.get("size")
                if isinstance(size, int) and size > cls.MAX_FILE_BYTES:
                    raise ValueError(f"Skill 单个文件超过大小限制：{path}")
                content_payload = api_json(path)
                encoded = content_payload.get("content") if isinstance(content_payload, dict) else None
                if isinstance(encoded, str):
                    try:
                        data = base64.b64decode(encoded.replace("\n", ""), validate=False)
                    except (ValueError, TypeError) as exc:
                        raise ValueError(f"GitHub 文件内容无法解码：{path}") from exc
                else:
                    download_url = payload.get("download_url")
                    if not isinstance(download_url, str):
                        raise ValueError(f"GitHub 文件没有可读取内容：{path}")
                    data, _, _ = cls._download_bytes(download_url, {"raw.githubusercontent.com"})
                if len(data) > cls.MAX_FILE_BYTES:
                    raise ValueError(f"Skill 单个文件超过大小限制：{path}")
                total += len(data)
                if total > cls.MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("Skill 解压后总大小超过限制。")
                files.append((relative or path.rsplit("/", 1)[-1], data))
                if len(files) > cls.MAX_FILES:
                    raise ValueError("Skill 文件数超过限制；请进一步缩小 GitHub tree 子目录。")
                return
            if not isinstance(payload, list):
                raise ValueError(f"GitHub 路径不是可读取的目录：{path}")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                item_path = str(item.get("path") or "")
                item_type = item.get("type")
                item_relative = item_path[len(clean_path):].lstrip("/") if item_path.startswith(clean_path) else item_path
                if item_type == "dir":
                    walk(item_path, item_relative)
                elif item_type == "file":
                    walk(item_path, item_relative)

        walk(clean_path)
        if not files:
            raise ValueError("GitHub Skill 子目录中没有可安装文件。")
        root_name = clean_path.rsplit("/", 1)[-1]
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, data in files:
                archive.writestr(f"{root_name}/{relative}", data)
        return out.getvalue(), "github-tree", f"https://github.com/{owner}/{repository}/tree/{ref}/{clean_path}"

    @classmethod
    def _download_clawhub(
        cls, slug: str, version: str, *, owner_handle: str | None = None,
    ) -> tuple[bytes, str, str]:
        if owner_handle is None and "/" in slug:
            owner_handle, slug = cls._split_clawhub_slug(slug)
        if owner_handle is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", owner_handle
        ):
            raise ValueError("ClawHub owner handle 鏃犳晥銆?")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?", slug):
            raise ValueError("ClawHub Skill slug 无效。")
        try:
            return cls._download_clawhub_once(slug, version, owner_handle=owner_handle)
        except SkillDownloadHTTPError as exc:
            if exc.status_code != 409:
                raise
            if owner_handle is None:
                candidate = cls._clawhub_search_owner(slug)
                if candidate:
                    try:
                        return cls._download_clawhub_once(
                            slug, version, owner_handle=candidate,
                        )
                    except SkillDownloadHTTPError:
                        pass
            # A few ClawHub entries have returned 409 for an unpinned latest
            # release while their metadata endpoint still exposes a concrete
            # version.  Retry exactly once with that version; never loop on a
            # persistent registry conflict.
            resolved_version = cls._clawhub_latest_version(slug, owner_handle=owner_handle)
            if version == "latest" and resolved_version:
                try:
                    return cls._download_clawhub_once(
                        slug, resolved_version, owner_handle=owner_handle,
                    )
                except SkillDownloadHTTPError as retry_error:
                    detail = retry_error.detail or exc.detail
                    raise ValueError(
                        f"ClawHub Skill {slug} 的版本 {resolved_version} 仍无法下载（HTTP 409 Conflict）。"
                        + (f"服务端说明：{detail}" if detail else "")
                        + "请稍后重试，或改用对应的 GitHub /tree/<ref>/<skill> 子目录 URL。"
                    ) from retry_error
            raise ValueError(
                f"ClawHub Skill {slug} 当前版本无法下载（HTTP 409 Conflict）。"
                + (f"服务端说明：{exc.detail}" if exc.detail else "")
                + "请稍后重试，或改用对应的 GitHub /tree/<ref>/<skill> 子目录 URL。"
            ) from exc

    @classmethod
    def _clawhub_search_owner(cls, slug: str) -> str | None:
        """Find the publisher for an exact ClawHub search hit."""
        url = (
            "https://clawhub.ai/api/v1/search?"
            f"q={quote(slug)}&limit=20"
        )
        try:
            blob, _, _ = cls._download_bytes(
                url, {"clawhub.ai", "www.clawhub.ai"}
            )
            payload = json.loads(blob.decode("utf-8"))
        except (SkillDownloadHTTPError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return None
        exact = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_slug = row.get("slug") or row.get("name")
            if str(candidate_slug or "").lower() != slug.lower():
                continue
            owner = row.get("ownerHandle") or row.get("owner")
            if isinstance(owner, dict):
                owner = owner.get("handle") or owner.get("name")
            if isinstance(owner, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", owner
            ):
                exact.append(owner)
        return exact[0] if len(set(exact)) == 1 else None

    @classmethod
    def _clawhub_latest_version(
        cls, slug: str, *, owner_handle: str | None = None,
    ) -> str | None:
        """Resolve the latest concrete version for a deterministic retry."""
        url = f"https://clawhub.ai/api/v1/skills/{quote(slug, safe='/-_.')}"
        if owner_handle:
            url += f"?ownerHandle={quote(owner_handle, safe='._-')}"
        try:
            blob, _, _ = cls._download_bytes(url, {"clawhub.ai", "www.clawhub.ai"})
            payload = json.loads(blob.decode("utf-8"))
        except (SkillDownloadHTTPError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        candidates = [
            payload.get("latestVersion") if isinstance(payload, dict) else None,
            payload.get("version") if isinstance(payload, dict) else None,
            payload.get("skill", {}).get("latestVersion") if isinstance(payload, dict) and isinstance(payload.get("skill"), dict) else None,
        ]
        for candidate in candidates:
            value = candidate.get("version") if isinstance(candidate, dict) else candidate
            if isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}", value):
                return value
        return None

    @classmethod
    def _download_clawhub_once(
        cls, slug: str, version: str, *, owner_handle: str | None = None,
    ) -> tuple[bytes, str, str]:
        query = f"slug={quote(slug, safe='/-_.')}"
        if owner_handle:
            query += f"&ownerHandle={quote(owner_handle, safe='._-')}"
        if version != "latest":
            query += f"&version={quote(version, safe='._-')}"
        api_url = f"https://clawhub.ai/api/v1/download?{query}"
        blob, content_type, resolved = cls._download_bytes(api_url, {"clawhub.ai", "www.clawhub.ai"})
        if "json" not in content_type.lower() and not blob.lstrip().startswith((b"{", b"[")):
            return blob, "clawhub", resolved
        try:
            payload = json.loads(blob.decode("utf-8"))
        except Exception as exc:
            raise ValueError("ClawHub 下载响应不是有效 ZIP 或 JSON。") from exc
        if isinstance(payload, dict) and str(payload.get("type", payload.get("packageType", ""))).lower() == "plugin":
            raise ValueError("出于安全原因，不允许安装 ClawHub plugin 包。")
        # GitHub-backed Skills return a structured handoff.  Prefer the exact
        # commit and subdirectory instead of downloading the whole repository.
        if isinstance(payload, dict):
            repository = payload.get("repo") or payload.get("repository")
            path = payload.get("path")
            commit = payload.get("commit") or version
            if isinstance(repository, str) and isinstance(path, str) and path.strip():
                match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?", repository.strip())
                if match:
                    return cls._download_github_subdir(
                        match.group(1), match.group(2),
                        commit if commit != "latest" else "main", path,
                    )
        handoff = None
        if isinstance(payload, dict):
            for key in ("downloadUrl", "download_url", "archiveUrl", "archive_url", "url", "repository", "repo"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    handoff = candidate
                    break
                # ClawHub's public-github handoff may provide owner/repo
                # separately instead of a fully-qualified URL.
                if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate):
                    handoff = "https://github.com/" + candidate
                    break
        if not handoff:
            raise ValueError("ClawHub 未返回可下载的 Skill ZIP。")
        parsed = urlparse(handoff)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {"github.com", "www.github.com", "codeload.github.com"}:
            raise ValueError("ClawHub 返回了不受信任的下载地址。")
        url = cls._github_url(handoff, version)
        archive, _, final_url = cls._download_bytes(url, {"github.com", "www.github.com", "codeload.github.com"})
        return archive, "clawhub-github", final_url

    def _install_archive(
        self,
        archive: bytes,
        *,
        allow_scripts: bool = False,
    ) -> str:
        cls = type(self)
        if not archive or len(archive) > cls.MAX_ARCHIVE_BYTES:
            raise ValueError("Skill 压缩包为空或超过大小限制。")
        staging_root = Path(tempfile.mkdtemp(prefix=".skill-install-"))
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                infos = zf.infolist()
                if len(infos) > cls.MAX_FILES:
                    raise ValueError(
                        f"Skill 文件数超过安全上限（{cls.MAX_FILES}）；"
                        "请改用 GitHub /tree/<ref>/<skill-subdir> 只安装目标 Skill。"
                    )
                total = 0
                seen: set[str] = set()
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    pure = Path(name)
                    if not name or "\x00" in name or pure.is_absolute() or any(part == ".." for part in pure.parts):
                        raise ValueError("Skill 压缩包包含不安全路径。")
                    if name in seen:
                        raise ValueError("Skill 压缩包包含重复路径。")
                    seen.add(name)
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        raise ValueError("Skill 压缩包不得包含符号链接。")
                    if info.file_size > cls.MAX_FILE_BYTES:
                        raise ValueError("Skill 单个文件超过大小限制。")
                    total += info.file_size
                    if total > cls.MAX_UNCOMPRESSED_BYTES:
                        raise ValueError("Skill 解压后总大小超过限制。")
                    if (
                        not allow_scripts
                        and not info.is_dir()
                        and Path(name).suffix.lower() in cls.SCRIPT_SUFFIXES
                    ):
                        raise ValueError(
                            "Skill 包含脚本文件，默认不安装："
                            f"{name}。如确认信任来源，请使用 "
                            "allow_scripts=true 重新发起安装并审批。"
                        )
                    target = staging_root.joinpath(*pure.parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst, length=64 * 1024)
            skill_files = list(staging_root.rglob("SKILL.md"))
            if len(skill_files) != 1:
                raise ValueError("Skill 包必须且只能包含一个 SKILL.md。")
            skill_file = skill_files[0]
            skill_root = skill_file.parent
            text = skill_file.read_text(encoding="utf-8")
            metadata = SkillRegistry._parse_frontmatter(text, skill_file)
            name = metadata.get("name", "")
            description = metadata.get("description", "")
            if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
                raise ValueError("SKILL.md 的 name 必须为小写字母、数字和连字符。")
            if not description:
                raise ValueError("SKILL.md 的 description 不能为空。")
            if skill_root.name != name:
                # GitHub source repositories commonly wrap a root Skill in
                # the archive directory (for example ``repo-main/``).  It is
                # safe to normalize that single wrapper, but do not accept a
                # deeply nested or ambiguous layout.
                if skill_root.parent != staging_root:
                    raise ValueError("SKILL.md 的 name 必须与目录名一致。")
                normalized_root = staging_root / name
                if normalized_root.exists():
                    raise ValueError("Skill 压缩包包含冲突的目录结构。")
                normalized_root.mkdir()
                for child in skill_root.iterdir():
                    shutil.move(str(child), str(normalized_root / child.name))
                shutil.rmtree(skill_root, ignore_errors=True)
                skill_root = normalized_root
            target = self.registry.skills_dir / name
            if target.exists():
                raise ValueError(f"Skill 已存在：{name}。请先删除旧版本后再安装。")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(skill_root), str(target))
            return name
        except zipfile.BadZipFile as exc:
            raise ValueError("下载内容不是有效 ZIP。") from exc
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)


    def activate(
        self,
        session_id: str,
        name: str,
        task: str,
        source: str,
        reason: str | None = None,
    ) -> dict:
        if source not in {"explicit", "auto"}:
            raise ValueError("Skill source 必须是 explicit 或 auto。")
        session = self.sessions.get(session_id)
        if session.active_skill:
            raise ValueError(f"当前已有 Active Skill：{session.active_skill['name']}")
        skill = self.registry.get(name)
        health = self.registry.diagnose(name)
        if health["status"] in {"unavailable", "needs_configuration"}:
            raise ValueError(f"Skill {name} {health['summary']}。请先在 Skills 面板查看体检结果。")
        usage_id = f"skilluse_{uuid.uuid4().hex[:12]}"
        usage = {
            "usageId": usage_id,
            "skillName": name,
            "sessionId": session_id,
            "task": task,
            "source": source,
            "reason": reason,
            "usedAt": utc_now(),
            "status": "active",
            "finalOutput": None,
            "savePath": None,
        }
        session.skill_usage.append(usage)
        session.active_skill = {
            "usageId": usage_id,
            "name": name,
            "description": skill.description,
            "source": source,
            "reason": reason,
            "task": task,
            "content": self.registry.load(name),
        }
        session.activity.append({
            "eventId": f"activity_{uuid.uuid4().hex[:12]}",
            "timestamp": utc_now(),
            "type": "skill_activated",
            "turnId": None,
            "data": {
                "usageId": usage_id,
                "skillName": name,
                "source": source,
                "reason": reason,
            },
        })
        self.sessions.save(session)
        return usage

    def activate_auto(
        self,
        context: ToolExecutionContext,
        name: str,
        reason: str,
    ) -> dict:
        session = self.sessions.get(context.session_id)
        task = self._latest_user_task(session)
        usage = self.activate(context.session_id, name, task, "auto", reason)
        return {
            "success": True,
            "usageId": usage["usageId"],
            "skillName": name,
            "source": "auto",
            "message": "Skill 已加载到当前 Agent Turn。",
        }

    def read_resource(
        self,
        context: ToolExecutionContext,
        path: str,
        offset: int = 0,
        max_chars: int = 12_000,
    ) -> dict:
        """Read one text resource from the active Skill with strict bounds."""
        session = self.sessions.get(context.session_id)
        active = session.active_skill
        if not active:
            raise ValueError("当前 Session 没有 Active Skill。")
        skill = self.registry.get(active["name"])
        relative = str(path or "").replace("\\", "/").strip("/")
        if not relative:
            raise ValueError("请从 Resource manifest 选择要读取的资源。")
        if relative.casefold() == "skill.md":
            # SKILL.md is already injected into the Active Skill context, but
            # some providers still request it explicitly. Treat that harmless
            # redundant read as an idempotent success instead of polluting the
            # Tool trace with a false failure.
            content = str(active.get("content") or self.registry.load(active["name"]))
            offset = max(0, int(offset))
            max_chars = max(1_000, min(int(max_chars), self.MAX_RESOURCE_CHUNK_CHARS))
            chunk = content[offset:offset + max_chars]
            next_offset = offset + len(chunk)
            return {
                "path": "SKILL.md",
                "offset": offset,
                "chars": len(chunk),
                "totalChars": len(content),
                "truncated": next_offset < len(content),
                "nextOffset": next_offset if next_offset < len(content) else None,
                "content": chunk,
                "alreadyInContext": True,
            }
        if relative.split("/", 1)[0] not in {"references", "scripts", "assets"}:
            raise ValueError("Skill 资源只能位于 references、scripts 或 assets。")
        root = skill.directory.resolve()
        target = (skill.directory / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Skill 资源路径越过了当前 Skill 边界。") from exc
        if not target.is_file():
            raise ValueError(f"Skill 资源不存在：{relative}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Skill 资源不是 UTF-8 文本，不能直接读取：{relative}") from exc
        offset = max(0, int(offset))
        max_chars = max(1_000, min(int(max_chars), self.MAX_RESOURCE_CHUNK_CHARS))
        chunk = content[offset:offset + max_chars]
        next_offset = offset + len(chunk)
        record = {
            "path": relative,
            "offset": offset,
            "chars": len(chunk),
            "totalChars": len(content),
            "truncated": next_offset < len(content),
            "nextOffset": next_offset if next_offset < len(content) else None,
        }
        reads = active.setdefault("resourceReads", [])
        reads.append({**record, "readAt": utc_now()})
        del reads[:-20]
        self.sessions.save(session)
        return {**record, "content": chunk}

    def run_script(
        self,
        context: ToolExecutionContext,
        path: str,
        args: list[str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict:
        """Run one approved Python/Node script bundled by the active Skill."""
        session = self.sessions.get(context.session_id)
        active = session.active_skill
        if not active:
            raise ValueError("当前 Session 没有 Active Skill。")
        skill = self.registry.get(active["name"])
        relative = str(path or "").replace("\\", "/").strip("/")
        if not relative.startswith("scripts/"):
            raise ValueError("只能执行当前 Skill 的 scripts/ 目录。")
        root = skill.directory.resolve()
        target = (skill.directory / relative).resolve()
        try:
            target.relative_to(root / "scripts")
        except ValueError as exc:
            raise ValueError("Skill 脚本路径越过了 scripts/ 边界。") from exc
        if not target.is_file():
            raise ValueError(f"Skill 脚本不存在：{relative}")
        suffix = target.suffix.lower()
        if suffix == ".py":
            command = [sys.executable, str(target)]
        elif suffix in {".js", ".mjs", ".cjs"}:
            node = shutil.which("node")
            if not node:
                raise ValueError("当前环境没有 Node.js，不能运行这个 Skill 脚本。")
            command = [node, str(target)]
        else:
            raise ValueError("仅支持执行 Skill 中的 Python 或 Node.js 脚本。")
        safe_args = []
        for value in args or []:
            item = str(value)
            if "\x00" in item:
                raise ValueError("Skill 脚本参数包含非法字符。")
            path_candidate = Path(item)
            if path_candidate.is_absolute() or ".." in path_candidate.parts:
                raise ValueError("Skill 脚本参数不能包含绝对路径或路径穿越。")
            safe_args.append(item)
        timeout_seconds = max(1, min(int(timeout_seconds), 180))
        env = {
            key: value for key, value in os.environ.items()
            if not any(
                marker in key.upper()
                for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
            )
        }
        env["SJTUCLAW_SKILL_DIR"] = str(skill.directory)
        cwd = Path(session.workspace) if session.workspace else skill.directory
        try:
            completed = subprocess.run(
                command + safe_args,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Skill 脚本执行超过 {timeout_seconds} 秒，已停止。",
                "path": relative,
            }
        stdout = (completed.stdout or "")[-40_000:]
        stderr = (completed.stderr or "")[-12_000:]
        return {
            "success": completed.returncode == 0,
            "path": relative,
            "exitCode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    @staticmethod
    def _latest_user_task(session: Session) -> str:
        for message in reversed(session.messages):
            if message.get("role") == "user" and not message.get("content", "").startswith("["):
                return message["content"]
        return "未找到原始任务"

    def finalize(self, session: Session, final_output: str) -> None:
        active = session.active_skill
        if not active:
            return
        usage = next(
            item for item in session.skill_usage if item["usageId"] == active["usageId"]
        )
        usage["status"] = "completed"
        usage["finalOutput"] = final_output
        usage["savePath"] = self._latest_saved_path(session, usage["usedAt"])
        session.activity.append({
            "eventId": f"activity_{uuid.uuid4().hex[:12]}",
            "timestamp": utc_now(),
            "type": "skill_completed",
            "turnId": None,
            "data": {
                "usageId": usage["usageId"],
                "skillName": usage["skillName"],
                "savePath": usage["savePath"],
            },
        })
        session.active_skill = None

    def fail_active(self, session_id: str, error: str) -> None:
        session = self.sessions.get(session_id)
        if not session.active_skill:
            return
        usage = next(
            item for item in session.skill_usage
            if item["usageId"] == session.active_skill["usageId"]
        )
        usage["status"] = "failed"
        usage["finalOutput"] = error
        session.active_skill = None
        self.sessions.save(session)

    @staticmethod
    def _latest_saved_path(session: Session, used_at: str) -> str | None:
        for event in reversed(session.tool_trace):
            if event.get("timestamp", "") < used_at:
                continue
            if event.get("tool") not in {"create_file", "overwrite_file", "edit_file"}:
                continue
            result = event.get("result", {})
            if result.get("success") and isinstance(result.get("output"), dict):
                return result["output"].get("path")
        return None


def register_skill_tool(registry: ToolRegistry, service: SkillService) -> None:
    """注册 Skill 选择、资源读取、执行与安装相关 Tools。"""

    registry.register(Tool(
        "read_skill_resource",
        (
            "按需读取当前 Active Skill 的一个文本资源。path 必须来自 Active Skill 的 "
            "Resource manifest；长文件使用 offset 和 nextOffset 分段读取。不要一次读取"
            "所有资源，也不要用它读取 Workspace 文件。"
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 40000,
                    "default": 12000,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        service.read_resource,
        "read_only",
        True,
        timeout_seconds=10,
    ))
    registry.register(Tool(
        "run_skill_script",
        (
            "执行当前 Active Skill 自带的一个 Python/Node 验证或生成脚本。path "
            "必须位于 Resource manifest 的 scripts/；参数以字符串数组传递，不经 "
            "Shell。每次执行都需要用户审批。优先用于 SKILL.md 明确要求的渲染、"
            "回算或验证步骤，不要运行无关脚本。"
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 180,
                    "default": 120,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        service.run_script,
        "approval_required",
        True,
        parallel_safe=False,
        side_effect=True,
        idempotent=False,
        timeout_seconds=190,
    ))
    registry.register(Tool(
        "use_skill",
        "请求加载一个与当前任务匹配的 Skill 指令。仅用于模型自主选择；必须说明 reason，批准后才会加载完整 Skill。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["name", "reason"],
            "additionalProperties": False,
        },
        service.activate_auto,
        "approval_required",
        True,
    ))
    registry.register(Tool(
        "install_skill",
        "从受信任的 GitHub 仓库/压缩包或 ClawHub 下载并安装 Skill。必须经过用户审批；仅接受 HTTPS，自动校验 SKILL.md、路径穿越、符号链接、大小和文件数量。默认拒绝脚本；只有用户在审批卡中明确确认 allow_scripts=true 才保留脚本文件，但安装过程仍不会执行它们。安装后刷新 Skill 索引。建议提供固定 ref/tag；ClawHub 使用 clawhub:<slug>，遇到未固定版本的 409 会自动解析具体版本重试；若注册中心仍不可用，请改用对应 GitHub /tree/<ref>/<skill-subdir> 子目录 URL。",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "version": {"type": "string"},
                "allow_scripts": {
                    "type": "boolean",
                    "description": "是否允许安装包内脚本；仅在信任来源且审批卡明确展示时设为 true。安装时不会执行脚本。",
                    "default": False,
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        service.install,
        "approval_required",
        True,
        parallel_safe=False,
        side_effect=True,
        idempotent=False,
    ))
