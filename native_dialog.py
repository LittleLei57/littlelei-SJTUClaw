"""Small local-only native dialogs used by the Web Gateway."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def pick_directory(initial: str | None = None) -> str | None:
    """Open the host OS folder picker and return an absolute path or None."""
    initial_path = str(Path(initial).resolve()) if initial else str(Path.home())
    if sys.platform == "win32":
        script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'SJTUClaw'
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择 SJTUClaw Workspace 文件夹'
$dialog.CheckFileExists = $false
$dialog.CheckPathExists = $true
$dialog.ValidateNames = $false
$dialog.DereferenceLinks = $true
$dialog.RestoreDirectory = $true
$dialog.FileName = '选择当前文件夹'
$dialog.Filter = '文件夹|*.folder|所有文件|*.*'
if (Test-Path -LiteralPath $env:SJTUCLAW_PICKER_INITIAL) {
  $dialog.InitialDirectory = $env:SJTUCLAW_PICKER_INITIAL
}
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
  $selected = [System.IO.Path]::GetDirectoryName($dialog.FileName)
  [Console]::Write($selected)
}
$owner.Close()
$owner.Dispose()
"""
        env = os.environ.copy()
        env["SJTUCLAW_PICKER_INITIAL"] = initial_path
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("目录选择超时，请重试或使用手动路径输入。") from exc
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "无法打开系统目录选择器。")
        selected = completed.stdout.strip()
        return selected or None

    # Best-effort fallback for desktop Linux/macOS Python installations.
    try:
        from tkinter import Tk, filedialog
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=initial_path, mustexist=True)
        root.destroy()
        return selected or None
    except Exception as exc:
        raise RuntimeError("当前环境无法打开系统目录选择器。") from exc
