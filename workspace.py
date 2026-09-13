"""Step 8：Session Workspace 配置与不可逃逸的路径解析。

每个 Session 独立记录一个绝对 Workspace 根目录，但 Tool 参数始终使用相对路径。
所有文件操作都通过 ``resolve()`` 规范化路径并验证其仍位于根目录内，从而拒绝
``..``、绝对路径和符号链接逃逸。Workspace 只决定 Agent 可操作的范围，不改变
项目源码目录，也不会因为进程当前工作目录变化而漂移。
"""

from pathlib import Path
from session_store import SessionStore, utc_now


class WorkspaceManager:
    """设置、读取并安全解析某个 Session 的 Workspace。"""

    def __init__(self, session_store: SessionStore, project_root: str | Path | None = None):
        self.session_store = session_store
        # Use the source tree, not process cwd, as the safe migration target.
        self.project_root = Path(project_root or Path(__file__).resolve().parent).expanduser().resolve()

    def set(self, session_id: str, path: str) -> str:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"Workspace 不存在：{target}")
        if not target.is_dir():
            raise NotADirectoryError(f"Workspace 不是目录：{target}")
        session = self.session_store.get(session_id)
        session.workspace = str(target)
        session.updated_at = utc_now()
        self.session_store.save(session)
        return session.workspace

    def get(self, session_id: str) -> Path:
        session = self.session_store.get(session_id)
        if not session.workspace:
            raise ValueError("当前 Session 尚未设置 Workspace。")
        root = Path(session.workspace).resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Workspace 不可用：{root}")
        return root

    def inspect(self, session_id: str) -> dict:
        """Return non-mutating Workspace health and migration information."""
        session = self.session_store.get(session_id)
        configured = session.workspace
        root = Path(configured).expanduser().resolve() if configured else None
        exists = bool(root and root.exists() and root.is_dir())
        candidate = self.project_root
        return {
            "sessionId": session_id,
            "workspace": configured,
            "exists": exists,
            "isDirectory": bool(root and root.is_dir()),
            "projectRoot": str(candidate),
            "canMigrate": bool(configured and not exists and candidate.is_dir() and root != candidate),
            "migrationHistory": list(getattr(session, "workspace_history", []) or []),
        }

    def migrate(self, session_id: str) -> dict:
        """Move a stale Workspace reference without touching user files."""
        session = self.session_store.get(session_id)
        if not session.workspace:
            raise ValueError("当前 Session 尚未设置 Workspace，请先使用 /workspace set")
        old_root = Path(session.workspace).expanduser().resolve()
        if old_root.exists() and old_root.is_dir():
            raise ValueError("当前 Workspace 仍然可用，无需迁移")
        target = self.project_root
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"当前项目目录不可用：{target}")
        if old_root == target:
            raise ValueError("Workspace 已经指向当前项目目录")
        history = list(getattr(session, "workspace_history", []) or [])
        history.append({"from": str(old_root), "to": str(target), "at": utc_now(), "reason": "stale_workspace_path"})
        session.workspace_history = history[-20:]
        session.workspace = str(target)
        session.updated_at = utc_now()
        self.session_store.save(session)
        return {
            "sessionId": session_id,
            "workspace": session.workspace,
            "previousWorkspace": str(old_root),
            "migrationHistory": session.workspace_history,
        }

    def resolve(
        self,
        session_id: str,
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        root = self.get(session_id)
        path = Path(relative_path).expanduser()
        if path.is_absolute():
            # Models frequently echo the Workspace path included in their context.
            # Treat an absolute path as a convenience only when its resolved target
            # remains inside this Session's Workspace; it never grants broader access.
            candidate = path.resolve(strict=False)
        else:
            candidate = (root / path).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            if path.is_absolute():
                raise ValueError("绝对路径不在当前 Workspace 内。") from exc
            raise ValueError("路径越过 Workspace 边界。") from exc
        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"Workspace 路径不存在：{relative_path}")
        return candidate

    def relative(self, session_id: str, path: Path) -> str:
        return str(path.resolve().relative_to(self.get(session_id)))
