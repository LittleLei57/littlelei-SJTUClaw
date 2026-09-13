"""Step 8：Workspace 文件的短期 Gateway 下载入口。

Agent 生成文件后不会直接暴露本机路径，而是创建随机、带过期时间的下载 token。
Gateway 校验 token 和目标文件后再返回内容；记录到期即失效，减少旧链接长期
暴露本地文件的风险。
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import RLock
import uuid

from session_store import utc_now


class DownloadStore:
    """持久化短期下载 token，并负责签发、解析和过期清理。"""

    def __init__(self, data_dir: str | Path, ttl_minutes: int = 15):
        self.path = Path(data_dir) / "downloads.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_minutes = ttl_minutes
        self._lock = RLock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Download JSON 损坏：{self.path}（{exc}）") from exc
        if not isinstance(data, list):
            raise ValueError("Download 数据必须是列表。")
        return data

    def _write(self, data: list[dict]) -> None:
        temp = self.path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temp.replace(self.path)

    def create(self, session_id: str, file_path: Path, filename: str) -> dict:
        now = datetime.now(timezone.utc)
        item = {
            "downloadId": f"dl_{uuid.uuid4().hex[:16]}",
            "sessionId": session_id,
            "path": str(file_path.resolve()),
            "filename": filename,
            "createdAt": utc_now(),
            "expiresAt": (now + timedelta(minutes=self.ttl_minutes)).isoformat(timespec="seconds"),
        }
        with self._lock:
            data = self._read()
            data.append(item)
            self._write(data)
        return {key: value for key, value in item.items() if key != "path"} | {
            "downloadUrl": f"/api/downloads/{item['downloadId']}"
        }

    def resolve(self, download_id: str) -> dict:
        with self._lock:
            item = next((row for row in self._read() if row.get("downloadId") == download_id), None)
        if item is None:
            raise KeyError(f"Download 不存在：{download_id}")
        expires = datetime.fromisoformat(item["expiresAt"])
        if expires <= datetime.now(timezone.utc):
            raise ValueError("Download 已过期。")
        path = Path(item["path"])
        if not path.exists() or not path.is_file():
            raise FileNotFoundError("Download 文件已不存在。")
        return item
