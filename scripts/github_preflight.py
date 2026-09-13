"""Audit Git candidates and index blobs without printing secret values."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import subprocess

try:
    from .publication_policy import forbidden_reason
except ImportError:
    from publication_policy import forbidden_reason

TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".ts", ".css", ".html", ".md", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".example", ".cmd", ".ps1",
    ".sh", ".svg", ".xml",
}
SECRET_PATTERNS = (
    re.compile(r"\b(?:sk-|tvly-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
PLACEHOLDERS = ("test", "example", "placeholder", "dummy", "redacted", "your-", "fake")
REQUIRED = ("README.md", ".gitignore", ".env.example", "requirements.txt")

def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True)
    if result.returncode:
        raise ValueError("Git 检查失败；请确认项目已初始化且 Git 可用。")
    return result.stdout

def git_candidates(root: Path) -> list[str]:
    top = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != root.resolve():
        raise ValueError("请在项目根目录初始化 Git，不要使用父目录的仓库。")
    raw = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({os.fsdecode(item) for item in raw.split(b"\0") if item})

def index_blobs(root: Path):
    records = []
    for row in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, oid, stage = meta.split()
        if stage != b"0":
            raise ValueError("Git 索引中存在未解决的合并冲突。")
        records.append((os.fsdecode(name), mode, oid))
    if not records:
        return
    raw = git(root, "cat-file", "--batch", data=b"\n".join(x[2] for x in records) + b"\n")
    offset = 0
    for name, mode, oid in records:
        end = raw.index(b"\n", offset)
        header = raw[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError("索引包含非普通文件或无法读取的对象。")
        size = int(header[2])
        offset = end + 1
        yield name, mode, raw[offset:offset + size]
        offset += size + 1

def local_secrets(root: Path) -> list[str]:
    path = root / ".env"
    values = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if len(value) >= 12 and any(x in key.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                values.append(value)
    return values

def contains_suspected_secret(text: str, known: list[str] = ()) -> bool:
    if any(value in text for value in known):
        return True
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if not any(marker in match.group().casefold() for marker in PLACEHOLDERS):
                return True
    return False

def audit(root: Path, *, exported: bool = False, max_mib: int = 20) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    for name in REQUIRED:
        if not (root / name).is_file():
            failures.append(f"缺少必要文件：{name}")
    known = local_secrets(root)
    def inspect(name: str, content: bytes, source: str):
        if len(content) > max_mib * 1024 * 1024:
            failures.append(f"{source}大文件：{name}")
        path = Path(name)
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", ".gitignore", ".dockerignore", "LICENSE"}:
            if contains_suspected_secret(content.decode("utf-8", errors="ignore"), known):
                failures.append(f"{source}疑似凭证：{name}")
    if exported:
        names = []
        for base, dirs, files in os.walk(root, followlinks=False):
            for directory in list(dirs):
                item = Path(base) / directory
                name = item.relative_to(root).as_posix()
                reason = forbidden_reason(name)
                if item.is_symlink() or item.is_junction() or reason:
                    failures.append(f"发布目录包含排除项：{name}")
                    dirs.remove(directory)
            names.extend((Path(base) / name).relative_to(root).as_posix() for name in files)
    else:
        names = git_candidates(root)
        for name, mode, content in index_blobs(root):
            if reason := forbidden_reason(name):
                failures.append(f"Git 已跟踪排除项：{name}（{reason}）")
            if mode not in {b"100644", b"100755"}:
                failures.append(f"Git 索引包含链接或非普通文件：{name}")
            inspect(name, content, "Git 索引")
    for name in names:
        if reason := forbidden_reason(name):
            failures.append(f"待发布文件被禁止：{name}（{reason}）")
            continue
        path = root / name
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()):
            failures.append(f"待发布路径包含链接或越界：{name}")
            continue
        if not path.is_file():
            failures.append(f"待发布文件缺失：{name}；请更新 Git 索引。")
            continue
        inspect(name, path.read_bytes(), "工作区")
    if not (root / "LICENSE").exists():
        warnings.append("主项目暂不授予开源许可；第三方资源按各自许可。")
    if (root / "web/vendor/katex").exists() and not (root / "web/vendor/katex/LICENSE").is_file():
        failures.append("缺少 KaTeX 许可证：web/vendor/katex/LICENSE")
    return sorted(set(failures)), warnings

def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Git 索引、待提交源码或干净发布目录。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--export", action="store_true", help="逐项检查无 Git 的发布目录，不应用忽略规则")
    parser.add_argument("--max-mib", type=int, default=20)
    args = parser.parse_args()
    try:
        failures, warnings = audit(args.root.resolve(), exported=args.export, max_mib=args.max_mib)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    for item in failures:
        print(f"[FAIL] {item}")
    for item in warnings:
        print(f"[WARN] {item}")
    print(f"结果：{'未通过' if failures else '通过'}；{len(failures)} 个阻塞项，{len(warnings)} 个提醒。")
    print("范围：当前源码与 Git 索引；不替代 Git 历史及图片内容审计。")
    return int(bool(failures))

if __name__ == "__main__":
    raise SystemExit(main())
