#!/usr/bin/env python3
"""SJTUClaw runtime backup, restore, integrity and redacted diagnostics.

The commands are deliberately independent from ``gateway`` so they never
start channels or the scheduler while maintaining local data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = Path(os.getenv("SJTUCLAW_DATA_DIR") or ROOT / "data")
DEFAULT_BACKUPS = ROOT / "backups"
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
SENSITIVE_NAMES = {".env", "weixin-account.json"}
SECRET_ENV_NAMES = (
    "LLM_API_KEY", "TAVILY_API_KEY", "FEISHU_APP_SECRET",
    "QQBOT_CLIENT_SECRET", "WEIXIN_TOKEN",
)


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gateway_running(host: str = "127.0.0.1", port: int = 8000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def snapshot_data(data_dir: Path, target: Path) -> list[dict]:
    if not data_dir.is_dir():
        raise FileNotFoundError(f"运行数据目录不存在：{data_dir}")
    target.mkdir(parents=True, exist_ok=True)
    for source in data_dir.rglob("*"):
        relative = source.relative_to(data_dir)
        if any(part in {"backups", "__pycache__"} for part in relative.parts):
            continue
        destination = target / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif source.suffix.lower() in SQLITE_SUFFIXES:
            sqlite_snapshot(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    manifest = []
    for path in sorted(item for item in target.rglob("*") if item.is_file()):
        if path.name == "manifest.json":
            continue
        manifest.append({
            "path": path.relative_to(target).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    (target / "manifest.json").write_text(json.dumps({
        "format": "sjtuclaw-backup-v1",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify_tree(root: Path) -> tuple[bool, list[str]]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return False, ["缺少 manifest.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"manifest 无法读取：{exc}"]
    errors: list[str] = []
    for item in manifest.get("files", []):
        relative = str(item.get("path") or "")
        path = root / relative
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            errors.append(f"非法路径：{relative}")
            continue
        if not path.is_file():
            errors.append(f"缺少文件：{relative}")
        elif path.stat().st_size != item.get("bytes"):
            errors.append(f"大小不符：{relative}")
        elif sha256(path) != item.get("sha256"):
            errors.append(f"校验失败：{relative}")
    return not errors, errors


def integrity_report(data_dir: Path) -> dict:
    report = {"dataDir": str(data_dir.resolve()), "ok": True, "issues": [], "sqlite": {}}
    if not data_dir.is_dir():
        report["ok"] = False
        report["issues"].append("运行数据目录不存在")
        return report
    for database in sorted(data_dir.glob("*.sqlite*")):
        connection = None
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
            row = connection.execute("PRAGMA integrity_check").fetchone()
            status = str(row[0] if row else "unknown")
        except sqlite3.Error as exc:
            status = f"error: {exc}"
        finally:
            if connection is not None:
                connection.close()
        report["sqlite"][database.name] = status
        if status.lower() != "ok":
            report["ok"] = False
            report["issues"].append(f"{database.name}: {status}")
    broken_json = []
    for path in data_dir.rglob("*.json"):
        if any(part in {"attachments", "workspace_archive", "backups"} for part in path.relative_to(data_dir).parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            broken_json.append(path.relative_to(data_dir).as_posix())
    if broken_json:
        report["ok"] = False
        report["issues"].extend(f"JSON 损坏：{item}" for item in broken_json)
    return report


def create_backup(data_dir: Path, backup_dir: Path, keep: int, *, quiet: bool = False) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    output = backup_dir / f"sjtuclaw-backup-{timestamp()}.zip"
    sequence = 2
    while output.exists():
        output = backup_dir / f"sjtuclaw-backup-{timestamp()}-{sequence}.zip"
        sequence += 1
    with tempfile.TemporaryDirectory(prefix="sjtuclaw-backup-") as temp:
        payload = Path(temp) / "data"
        files = snapshot_data(data_dir, payload)
        shutil.make_archive(str(output.with_suffix("")), "zip", Path(temp))
    if keep > 0:
        archives = sorted(backup_dir.glob("sjtuclaw-backup-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in archives[keep:]:
            stale.unlink(missing_ok=True)
    if not quiet:
        print(f"[完成] 备份：{output}（{len(files)} 个文件，滚动保留 {keep} 份）")
    return output


def verify_backup(archive: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="sjtuclaw-verify-") as temp:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)
        ok, errors = verify_tree(Path(temp) / "data")
    if ok:
        print(f"[通过] 备份完整：{archive}")
    else:
        print(f"[失败] 备份损坏：{archive}")
        for error in errors[:20]:
            print(f"  - {error}")
    return ok


def restore_backup(archive: Path, data_dir: Path, backup_dir: Path, keep: int, force: bool) -> None:
    if gateway_running() and not force:
        raise RuntimeError("检测到 Gateway 正在运行。请先关闭 Gateway；确认无写入后可使用 --force。")
    if not verify_backup(archive):
        raise RuntimeError("备份校验未通过，拒绝恢复。")
    if data_dir.exists() and any(data_dir.iterdir()):
        safety = create_backup(data_dir, backup_dir, keep, quiet=True)
        print(f"[保护] 恢复前快照：{safety}")
    with tempfile.TemporaryDirectory(prefix="sjtuclaw-restore-") as temp:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)
        restored = Path(temp) / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        shutil.copytree(restored, data_dir)
        (data_dir / "manifest.json").unlink(missing_ok=True)
    report = integrity_report(data_dir)
    if not report["ok"]:
        raise RuntimeError("恢复完成但完整性检查失败：" + "；".join(report["issues"]))
    print(f"[完成] 已恢复到：{data_dir}")


def package_versions() -> dict:
    names = ("openai", "fastapi", "uvicorn", "numpy", "Pillow", "pypdf", "PyMuPDF", "rich", "sympy")
    result = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "未安装"
    return result


def sqlite_counts(path: Path) -> dict:
    if not path.is_file():
        return {}
    counts = {}
    connection = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            safe = table.replace('"', '""')
            counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0]
    except sqlite3.Error as exc:
        counts["error"] = type(exc).__name__
    finally:
        if connection is not None:
            connection.close()
    return counts


def create_diagnostic(data_dir: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"sjtuclaw-diagnostic-{timestamp()}.zip"
    integrity = integrity_report(data_dir)
    report = {
        "format": "sjtuclaw-redacted-diagnostic-v1",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "python": platform.python_version(), "architecture": platform.machine(),
        },
        "gatewayReachable": gateway_running(),
        "integrity": integrity,
        "runtimeCounts": {
            path.name: sqlite_counts(path) for path in data_dir.glob("*.sqlite*")
        } if data_dir.exists() else {},
        "sessionDirectories": len(list((data_dir / "sessions").glob("*"))) if (data_dir / "sessions").is_dir() else 0,
        "attachmentFiles": len(list((data_dir / "attachments").rglob("*"))) if (data_dir / "attachments").is_dir() else 0,
        "configured": {name: bool(os.getenv(name)) for name in SECRET_ENV_NAMES},
        "packages": package_versions(),
    }
    with tempfile.TemporaryDirectory(prefix="sjtuclaw-diagnostic-") as temp:
        root = Path(temp)
        (root / "diagnostic.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in ("README.md", "ROADMAP.md", "requirements.txt", "requirements-lock.txt"):
            source = ROOT / name
            if source.is_file():
                shutil.copy2(source, root / name)
        shutil.make_archive(str(output.with_suffix("")), "zip", root)
    print(f"[完成] 脱敏诊断包：{output}")
    print("       不含 .env、凭证、消息正文、Memory 正文或附件正文。")
    return output


def retention_preview(
    data_dir: Path,
    *,
    older_than_days: int = 30,
    keep_turns_per_session: int = 100,
) -> dict:
    """Return a non-destructive cleanup plan for durable execution history.

    Conversation messages, summaries, attachments, Memory, scheduled tasks,
    pending approvals and active Tool executions are deliberately out of
    scope.  Only old terminal Turn journals and completed Tool idempotency
    records can become candidates.
    """
    days = max(1, int(older_than_days))
    keep = max(1, int(keep_turns_per_session))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    report = {
        "mode": "preview",
        "cutoff": cutoff,
        "olderThanDays": days,
        "keepTurnsPerSession": keep,
        "turns": 0,
        "turnEvents": 0,
        "toolExecutions": 0,
        "databaseBytes": {},
        "protected": [
            "会话正文、Summary、附件与 Memory",
            "运行中/等待审批的 Turn",
            "执行中或状态不明确的副作用 Tool",
            "Scheduler、Approval 与渠道投递状态",
        ],
        "_turnIds": [],
        "_toolExecutionKeys": [],
    }
    turns_path = data_dir / "turns.sqlite3"
    if turns_path.is_file():
        report["databaseBytes"][turns_path.name] = turns_path.stat().st_size
        # Preview must remain usable while Gateway owns the WAL/SHM files.
        # ``immutable=1`` prevents SQLite from attempting any sidecar write.
        connection = sqlite3.connect(
            f"{turns_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if {"turns", "turn_events"} <= tables:
                rows = connection.execute(
                    """
                    WITH ranked AS (
                        SELECT turn_id, session_id, status, updated_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY session_id
                                   ORDER BY updated_at DESC, turn_id DESC
                               ) AS position
                        FROM turns
                        WHERE status IN (
                            'completed', 'cancelled', 'error',
                            'failed', 'interrupted'
                        )
                    )
                    SELECT turn_id FROM ranked
                    WHERE position > ? AND julianday(updated_at) < julianday(?)
                    ORDER BY updated_at ASC
                    """,
                    (keep, cutoff),
                ).fetchall()
                turn_ids = [str(row[0]) for row in rows]
                report["_turnIds"] = turn_ids
                report["turns"] = len(turn_ids)
                if turn_ids:
                    report["turnEvents"] = sum(
                        connection.execute(
                            "SELECT COUNT(*) FROM turn_events WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchone()[0]
                        for turn_id in turn_ids
                    )
        finally:
            connection.close()
    state_path = data_dir / "state.sqlite3"
    if state_path.is_file():
        report["databaseBytes"][state_path.name] = state_path.stat().st_size
        connection = sqlite3.connect(
            f"{state_path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tool_executions'"
            ).fetchone()
            if exists:
                rows = connection.execute(
                    """
                    SELECT execution_key FROM tool_executions
                    WHERE status = 'completed'
                      AND completed_at IS NOT NULL
                      AND julianday(completed_at) < julianday(?)
                    ORDER BY completed_at ASC
                    """,
                    (cutoff,),
                ).fetchall()
                keys = [str(row[0]) for row in rows]
                report["_toolExecutionKeys"] = keys
                report["toolExecutions"] = len(keys)
        finally:
            connection.close()
    return report


def apply_retention_cleanup(
    data_dir: Path,
    backup_dir: Path,
    *,
    older_than_days: int = 30,
    keep_turns_per_session: int = 100,
    force: bool = False,
) -> dict:
    """Apply exactly one previously computed class of safe history cleanup."""
    if gateway_running() and not force:
        raise RuntimeError(
            "检测到 Gateway 正在运行。历史清理默认只提供预览；"
            "请先关闭 Gateway，确认后再使用 --apply。"
        )
    report = retention_preview(
        data_dir,
        older_than_days=older_than_days,
        keep_turns_per_session=keep_turns_per_session,
    )
    turn_ids = list(report.pop("_turnIds"))
    execution_keys = list(report.pop("_toolExecutionKeys"))
    if not turn_ids and not execution_keys:
        report["mode"] = "applied"
        report["backupDir"] = None
        return report
    safety_dir = backup_dir / f"history-cleanup-{timestamp()}"
    safety_dir.mkdir(parents=True, exist_ok=True)
    for name in ("turns.sqlite3", "state.sqlite3"):
        source = data_dir / name
        if source.is_file():
            sqlite_snapshot(source, safety_dir / name)
    if turn_ids:
        connection = sqlite3.connect(data_dir / "turns.sqlite3", timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM turns WHERE turn_id = ?",
                ((turn_id,) for turn_id in turn_ids),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    if execution_keys:
        connection = sqlite3.connect(data_dir / "state.sqlite3", timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM tool_executions WHERE execution_key = ?",
                ((key,) for key in execution_keys),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    report["mode"] = "applied"
    report["backupDir"] = str(safety_dir)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SJTUClaw 运行数据维护")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="创建一致性备份并滚动保留")
    backup.add_argument("--keep", type=int, default=5)
    verify = sub.add_parser("verify", help="检查运行数据或备份")
    verify.add_argument("archive", nargs="?", type=Path)
    restore = sub.add_parser("restore", help="从备份恢复（应先关闭 Gateway）")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--keep", type=int, default=5)
    restore.add_argument("--force", action="store_true")
    diagnostic = sub.add_parser("diagnostic", help="生成不含用户正文和密钥的诊断包")
    diagnostic.add_argument("--output-dir", type=Path, default=ROOT / "diagnostics")
    cleanup = sub.add_parser(
        "cleanup-history",
        help="预览旧 Turn/Trace 与已完成 Tool 记录；加 --apply 才删除",
    )
    cleanup.add_argument("--older-than-days", type=int, default=30)
    cleanup.add_argument("--keep-turns-per-session", type=int, default=100)
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument(
        "--force",
        action="store_true",
        help="Gateway 运行时仍执行（不推荐，仅用于受控维护）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "backup":
            archive = create_backup(args.data_dir.resolve(), args.backup_dir.resolve(), max(1, args.keep))
            return 0 if verify_backup(archive) else 1
        if args.command == "verify":
            if args.archive:
                return 0 if verify_backup(args.archive.resolve()) else 1
            report = integrity_report(args.data_dir.resolve())
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "restore":
            restore_backup(
                args.archive.resolve(), args.data_dir.resolve(), args.backup_dir.resolve(),
                max(1, args.keep), args.force,
            )
            return 0
        if args.command == "diagnostic":
            create_diagnostic(args.data_dir.resolve(), args.output_dir.resolve())
            return 0
        if args.command == "cleanup-history":
            if args.apply:
                report = apply_retention_cleanup(
                    args.data_dir.resolve(),
                    args.backup_dir.resolve(),
                    older_than_days=args.older_than_days,
                    keep_turns_per_session=args.keep_turns_per_session,
                    force=args.force,
                )
            else:
                report = retention_preview(
                    args.data_dir.resolve(),
                    older_than_days=args.older_than_days,
                    keep_turns_per_session=args.keep_turns_per_session,
                )
                report.pop("_turnIds", None)
                report.pop("_toolExecutionKeys", None)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not args.apply:
                print("提示：当前仅为预览；确认后追加 --apply 才会删除候选历史。")
            return 0
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
