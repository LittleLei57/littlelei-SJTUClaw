"""Step 8：按 Session 维护的持久 PowerShell。

Shell 创建时把 cwd 固定到该 Session 的 Workspace，后续命令不会接受任意宿主
工作目录。stdout/stderr 由后台线程持续抽取并限制总长度，命令执行带超时，
Session 删除或 Gateway 关闭时会回收子进程，防止遗留后台 Shell。
"""

from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Lock, Thread
import time
import uuid

from workspace import WorkspaceManager


@dataclass
class ShellSession:
    """一个 PowerShell 子进程及其异步输出队列和当前目录。"""

    process: subprocess.Popen
    stdout: Queue
    stderr: Queue
    lock: Lock
    cwd: Path


class ShellManager:
    """创建、复用、执行和关闭 Session 隔离的 PowerShell。"""

    MAX_OUTPUT = 20_000

    def __init__(self, workspaces: WorkspaceManager):
        self.workspaces = workspaces
        self._shells: dict[str, ShellSession] = {}
        self._manager_lock = Lock()

    @staticmethod
    def _reader(pipe, queue: Queue) -> None:
        try:
            for line in iter(pipe.readline, ""):
                queue.put(line)
        finally:
            pipe.close()

    def new_shell(self, session_id: str, cwd: str = ".") -> dict:
        target = self.workspaces.resolve(session_id, cwd, must_exist=True)
        if not target.is_dir():
            raise NotADirectoryError(f"Shell cwd 不是目录：{cwd}")
        self.close(session_id)
        process = subprocess.Popen(
            ["powershell", "-NoLogo", "-NoProfile", "-Command", "-"],
            cwd=target,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout_queue: Queue = Queue()
        stderr_queue: Queue = Queue()
        Thread(target=self._reader, args=(process.stdout, stdout_queue), daemon=True).start()
        Thread(target=self._reader, args=(process.stderr, stderr_queue), daemon=True).start()
        with self._manager_lock:
            self._shells[session_id] = ShellSession(
                process, stdout_queue, stderr_queue, Lock(), target.resolve()
            )
        return {"success": True, "cwd": str(target.resolve()), "message": "Shell 已启动。"}

    def run_command(self, session_id: str, command: str, timeout_seconds: int = 30) -> dict:
        shell = self._shells.get(session_id)
        if shell is None or shell.process.poll() is not None:
            self.close(session_id)
            raise RuntimeError("当前没有可用 Shell，请先调用 new_shell。")
        root = self.workspaces.get(session_id)
        self._ensure_inside(root, shell.cwd)
        marker = uuid.uuid4().hex
        stdout_end = f"__SJTUCLAW_OUT_{marker}__"
        stderr_end = f"__SJTUCLAW_ERR_{marker}__"
        script = (
            f"{command}\n"
            "$__sjtu_code = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }\n"
            f"Write-Output \"{stdout_end}$__sjtu_code|$((Get-Location).Path)\"\n"
            f"[Console]::Error.WriteLine(\"{stderr_end}\")\n"
        )
        started_cwd = shell.cwd
        with shell.lock:
            try:
                shell.process.stdin.write(script)
                shell.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.close(session_id)
                raise RuntimeError("Shell 已退出，请重新调用 new_shell。") from exc
            stdout, stderr, exit_code, final_cwd, timed_out = self._collect(
                shell, stdout_end, stderr_end, timeout_seconds
            )
        if timed_out:
            self.close(session_id)
            return self._result(command, started_cwd, None, stdout, stderr, True, "命令超时，Shell 已终止。")
        try:
            final_path = Path(final_cwd).resolve()
            self._ensure_inside(root, final_path)
        except Exception:
            self.close(session_id)
            return self._result(command, started_cwd, exit_code, stdout, stderr, False, "Shell 离开 Workspace，已终止。")
        shell.cwd = final_path
        return self._result(command, started_cwd, exit_code, stdout, stderr, False, None)

    def _collect(self, shell, stdout_end, stderr_end, timeout_seconds):
        deadline = time.monotonic() + max(1, min(timeout_seconds, 300))
        stdout_parts, stderr_parts = [], []
        out_done = err_done = False
        exit_code = None
        final_cwd = str(shell.cwd)
        while time.monotonic() < deadline and not (out_done and err_done):
            try:
                line = shell.stdout.get(timeout=0.05)
                if line.startswith(stdout_end):
                    payload = line[len(stdout_end):].strip()
                    code, _, final_cwd = payload.partition("|")
                    exit_code = int(code)
                    out_done = True
                else:
                    stdout_parts.append(line)
            except Empty:
                pass
            try:
                line = shell.stderr.get_nowait()
                if line.strip() == stderr_end:
                    err_done = True
                else:
                    stderr_parts.append(line)
            except Empty:
                pass
            if shell.process.poll() is not None and shell.stdout.empty() and shell.stderr.empty():
                break
        return "".join(stdout_parts), "".join(stderr_parts), exit_code, final_cwd, not (out_done and err_done)

    def _result(self, command, cwd, exit_code, stdout, stderr, timed_out, error):
        truncated = len(stdout) > self.MAX_OUTPUT or len(stderr) > self.MAX_OUTPUT
        effective_error = error
        if effective_error is None and exit_code not in {0, None}:
            effective_error = f"命令退出码为 {exit_code}。"
        return {
            "success": effective_error is None and exit_code == 0,
            "command": command,
            "cwd": str(cwd),
            "exitCode": exit_code,
            "stdout": stdout[: self.MAX_OUTPUT],
            "stderr": stderr[: self.MAX_OUTPUT],
            "timedOut": timed_out,
            "truncated": truncated,
            "error": effective_error,
        }

    @staticmethod
    def _ensure_inside(root: Path, cwd: Path) -> None:
        try:
            cwd.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("Shell cwd 位于 Workspace 外。") from exc

    def close(self, session_id: str) -> None:
        with self._manager_lock:
            shell = self._shells.pop(session_id, None)
        if not shell:
            return
        if shell.process.stdin and not shell.process.stdin.closed:
            shell.process.stdin.close()
        if shell.process.poll() is None:
            shell.process.terminate()
            try:
                shell.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                shell.process.kill()
                shell.process.wait(timeout=2)

    def close_all(self) -> None:
        for session_id in list(self._shells):
            self.close(session_id)
