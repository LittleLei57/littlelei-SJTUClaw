"""Shared, conservative file policy for public source exports."""
from pathlib import PurePosixPath

BUILTIN_SKILLS = {
    "course-report", "document-reader", "material-summary", "pdf-reader",
    "presentation-outline", "repository-briefing",
}
PRIVATE_PARTS = {
    ".git", ".env", ".venv", ".codex", ".agents", ".vscode", ".idea",
    "__pycache__", ".pytest_cache", "node_modules", "data", "workspace",
    "logs", "backups", "diagnostics", "reference_materials", "dist", "build",
    "release", "out", "coverage", "htmlcov",
}
PRIVATE_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".bak", ".tmp", ".zip", ".7z", ".rar",
    ".exe", ".msi", ".sqlite", ".sqlite3", ".db",
}

def forbidden_reason(name: str) -> str | None:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return "路径越界"
    if any(part.casefold() in PRIVATE_PARTS for part in path.parts):
        return "本地数据、依赖或构建目录"
    if path.name in {"sjtu_api_key", ".coverage"}:
        return "本地凭证或覆盖率数据"
    if path.name.startswith(".env.") and path.name != ".env.example":
        return "环境凭证"
    if path.suffix.lower() in PRIVATE_SUFFIXES or any(x in path.name.lower() for x in (".sqlite-", ".sqlite3-", ".db-")):
        return "运行数据或构建产物"
    if path.parts and path.parts[0].lower() == "skills":
        if len(path.parts) > 1 and path.parts[1] not in BUILTIN_SKILLS | {"README.md"}:
            return "未列入发布白名单的 Skill"
    if path.suffix.lower() in {".pdf", ".docx"} and (len(path.parts) == 1 or path.parts[0] == "docs"):
        return "未审核的二进制文档（发布 Markdown 源文档）"
    return None
