"""Step 6：与 Session 隔离的附件元数据和内容寻址存储。

上传内容按 SHA-256 写入共享 blob，文件名、大小和引用只记录在所属 Session；
相同文件无需重复落盘，不同 Session 也不能枚举彼此附件。删除引用时仅在没有
其他 Session 使用该 blob 后清理磁盘内容。
"""

from pathlib import Path
from typing import BinaryIO
import hashlib
import uuid
from functools import wraps
from threading import RLock

from session_store import SessionStore, utc_now


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class AttachmentStore:
    """校验上传、维护 Session 引用并管理去重 blob。"""

    MAX_BYTES = 10 * 1024 * 1024
    MAX_PER_SESSION = 20

    def __init__(self, session_store: SessionStore):
        self.session_store = session_store
        self._lock = RLock()
        self.blob_dir = self.session_store.data_dir / "attachments" / "blobs"
        self.temp_dir = self.session_store.data_dir / "attachments" / "tmp"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.migrate_legacy_files()

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(char for char in name if char.isprintable() and char not in '<>:"/\\|?*').strip()
        return name[:200] or "attachment"

    @_locked
    def save(
        self,
        session_id: str,
        filename: str,
        content_type: str | None,
        stream: BinaryIO,
    ) -> dict:
        session = self.session_store.get(session_id)
        if len(session.attachments) >= self.MAX_PER_SESSION:
            raise ValueError(f"当前 Session 附件数量已达上限：{self.MAX_PER_SESSION}。")
        attachment_id = f"att_{uuid.uuid4().hex[:12]}"
        safe_name = self._safe_filename(filename)
        temporary = self.temp_dir / f"upload_{uuid.uuid4().hex}.tmp"
        size = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("wb") as output:
                while chunk := stream.read(64 * 1024):
                    size += len(chunk)
                    if size > self.MAX_BYTES:
                        raise ValueError(f"附件超过 {self.MAX_BYTES} 字节限制。")
                    digest.update(chunk)
                    output.write(chunk)
            sha256 = digest.hexdigest()
            target = self.blob_dir / sha256
            if target.exists():
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(target)
            metadata = {
                "attachmentId": attachment_id,
                "filename": safe_name,
                "size": size,
                "contentType": content_type or "application/octet-stream",
                "uploadedAt": utc_now(),
                "sha256": sha256,
                "storage": "blob",
            }
            session.attachments.append(metadata)
            session.updated_at = utc_now()
            self.session_store.save(session)
            return metadata
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def list(self, session_id: str) -> list[dict]:
        session = self.session_store.get(session_id)
        return [
            {**item, "available": resolve_attachment_path(self.session_store, session_id, item).is_file()}
            for item in session.attachments
        ]

    def get(self, session_id: str, attachment_id: str) -> tuple[dict, Path]:
        session = self.session_store.get(session_id)
        metadata = next(
            (item for item in session.attachments if item.get("attachmentId") == attachment_id),
            None,
        )
        if metadata is None:
            raise KeyError(f"当前 Session 没有附件：{attachment_id}")
        path = resolve_attachment_path(self.session_store, session_id, metadata)
        if not path.is_file():
            raise FileNotFoundError("附件内容已经不存在。")
        return metadata, path

    @_locked
    def delete(self, session_id: str, attachment_id: str, delete_file: bool = False) -> dict:
        """Remove an attachment reference, optionally deleting its stored bytes."""
        session = self.session_store.get(session_id)
        metadata = next(
            (item for item in session.attachments if item.get("attachmentId") == attachment_id),
            None,
        )
        if metadata is None:
            raise KeyError(f"当前 Session 没有附件：{attachment_id}")
        file_path = resolve_attachment_path(self.session_store, session_id, metadata)
        file_existed = file_path.is_file()
        session.attachments = [
            item for item in session.attachments
            if item.get("attachmentId") != attachment_id
        ]
        session.updated_at = utc_now()
        self.session_store.save(session)
        retained_by_others = False
        if delete_file:
            sha256 = metadata.get("sha256")
            if sha256:
                retained_by_others = self.reference_count(str(sha256)) > 0
            if not retained_by_others:
                file_path.unlink(missing_ok=True)
        return {
            "attachment": metadata,
            "metadataDeleted": True,
            "fileDeleted": bool(delete_file and file_existed and not retained_by_others),
            "fileRetained": bool(file_existed and (not delete_file or retained_by_others)),
            "remainingReferences": self.reference_count(str(metadata.get("sha256"))) if metadata.get("sha256") else 0,
        }

    def reference_count(self, sha256: str) -> int:
        return sum(
            1
            for session in self.session_store.list_sessions()
            for item in session.attachments
            if item.get("sha256") == sha256
        )

    @_locked
    def delete_session(self, session_id: str) -> dict:
        """Delete a Session and release every attachment reference it owned.

        Session metadata is the reference table for the shared content-addressed
        blob directory. Deleting its JSON directly would leave unique blobs
        orphaned, so references are snapshotted before the Session is removed.
        """
        session = self.session_store.get(session_id)
        attachments = [dict(item) for item in session.attachments]
        paths = [
            resolve_attachment_path(self.session_store, session_id, item)
            for item in attachments
        ]
        current_id = self.session_store.delete(session_id)
        deleted_blobs: set[str] = set()
        retained_blobs: set[str] = set()
        deleted_legacy_files = 0
        for metadata, path in zip(attachments, paths):
            sha256 = str(metadata.get("sha256") or "")
            if sha256:
                if sha256 in deleted_blobs or sha256 in retained_blobs:
                    continue
                if self.reference_count(sha256) > 0:
                    retained_blobs.add(sha256)
                else:
                    path.unlink(missing_ok=True)
                    deleted_blobs.add(sha256)
            elif path.is_file():
                path.unlink(missing_ok=True)
                deleted_legacy_files += 1
        return {
            "deletedSessionId": session_id,
            "currentSessionId": current_id,
            "releasedAttachmentReferences": len(attachments),
            "deletedBlobs": len(deleted_blobs),
            "retainedSharedBlobs": len(retained_blobs),
            "deletedLegacyFiles": deleted_legacy_files,
        }

    @_locked
    def migrate_legacy_files(self) -> int:
        """Move old per-Session attachment files into the shared blob store."""
        migrated = 0
        for session in self.session_store.list_sessions():
            changed = False
            for item in session.attachments:
                if item.get("sha256"):
                    continue
                legacy = self.session_store.sessions_dir / session.session_id / "attachments" / str(item.get("attachmentId"))
                if not legacy.is_file():
                    continue
                sha256 = _sha256_file(legacy)
                blob = self.blob_dir / sha256
                if blob.exists():
                    legacy.unlink()
                else:
                    legacy.replace(blob)
                item["sha256"] = sha256
                item["storage"] = "blob"
                changed = True
                migrated += 1
            if changed:
                self.session_store.save(session)
        return migrated

    @_locked
    def audit(self, cleanup_orphans: bool = False) -> dict:
        referenced: set[str] = set()
        missing: list[dict] = []
        for session in self.session_store.list_sessions():
            for item in session.attachments:
                sha256 = item.get("sha256")
                if sha256:
                    referenced.add(str(sha256))
                path = resolve_attachment_path(self.session_store, session.session_id, item)
                if not path.is_file():
                    missing.append({
                        "sessionId": session.session_id,
                        "attachmentId": item.get("attachmentId"),
                        "filename": item.get("filename"),
                    })
        orphan_paths = [path for path in self.blob_dir.iterdir() if path.is_file() and path.name not in referenced]
        removed = []
        if cleanup_orphans:
            for path in orphan_paths:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        return {
            "referencedBlobs": len(referenced),
            "missingReferences": missing,
            "orphanBlobs": [path.name for path in orphan_paths],
            "removedOrphans": removed,
        }


def resolve_attachment_path(session_store: SessionStore, session_id: str, metadata: dict) -> Path:
    """只在附件确实属于指定 Session 时返回其磁盘路径。"""

    sha256 = metadata.get("sha256")
    if sha256:
        return session_store.data_dir / "attachments" / "blobs" / str(sha256)
    return session_store.sessions_dir / session_id / "attachments" / str(metadata.get("attachmentId"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
