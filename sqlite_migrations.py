"""Small, ordered and rollback-safe SQLite schema migrations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import sqlite3


MigrationHandler = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class SQLiteMigration:
    version: int
    name: str
    apply: MigrationHandler


@dataclass(frozen=True)
class MigrationResult:
    previous_version: int
    current_version: int
    applied: tuple[str, ...]
    backup_path: Path | None = None


def _backup_path(path: Path, previous_version: int, target_version: int) -> Path:
    return path.with_name(
        f"{path.name}.pre-migration-v{previous_version}-to-v{target_version}.bak"
    )


def _create_consistent_backup(source_path: Path, destination_path: Path) -> None:
    """Use SQLite's backup API so WAL content is included in the snapshot."""
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=10)
    destination = sqlite3.connect(temporary, timeout=10)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    temporary.replace(destination_path)


def run_sqlite_migrations(
    path: str | Path,
    migrations: Sequence[SQLiteMigration],
    *,
    timeout: float = 10,
) -> MigrationResult:
    """Apply ordered migrations atomically and retain a pre-upgrade backup.

    A database newer than the running code is rejected instead of being
    silently downgraded. Existing databases are backed up once for each
    version transition. All pending migrations share one transaction, so a
    failure leaves both the schema and ``PRAGMA user_version`` unchanged.
    """

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = [item.version for item in ordered]
    if versions != list(range(1, len(ordered) + 1)):
        raise ValueError("SQLite migration versions must be consecutive from 1")
    target_version = versions[-1] if versions else 0
    existed_with_data = database_path.exists() and database_path.stat().st_size > 0

    connection: sqlite3.Connection | None = sqlite3.connect(
        database_path, timeout=timeout
    )
    connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    backup_path: Path | None = None
    try:
        previous_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if previous_version > target_version:
            raise RuntimeError(
                "SQLite 状态库版本高于当前程序支持范围："
                f"{previous_version} > {target_version}"
            )
        pending = tuple(
            item for item in ordered if item.version > previous_version
        )
        if not pending:
            return MigrationResult(
                previous_version,
                previous_version,
                (),
                None,
            )

        # Close the writer before copying so the backup API sees a clean,
        # stable source connection. A brand-new empty database needs no backup.
        connection.close()
        connection = None
        if existed_with_data:
            backup_path = _backup_path(
                database_path, previous_version, target_version
            )
            if not backup_path.exists():
                _create_consistent_backup(database_path, backup_path)

        connection = sqlite3.connect(database_path, timeout=timeout)
        connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        connection.execute("BEGIN EXCLUSIVE")
        # Another process may have migrated while this process made a backup.
        locked_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        pending = tuple(item for item in ordered if item.version > locked_version)
        applied: list[str] = []
        for migration in pending:
            migration.apply(connection)
            connection.execute(f"PRAGMA user_version={migration.version}")
            applied.append(migration.name)
        connection.commit()
        return MigrationResult(
            previous_version,
            target_version,
            tuple(applied),
            backup_path,
        )
    except Exception:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
        # A failed temporary backup must never look like a valid restore point.
        temporary = (
            backup_path.with_suffix(backup_path.suffix + ".tmp")
            if backup_path is not None
            else None
        )
        if temporary is not None:
            temporary.unlink(missing_ok=True)
