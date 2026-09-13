"""SJTUClaw environment doctor, installer, and process launcher.

This module intentionally uses only the Python standard library so ``doctor``
still works before the project dependencies have been installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


BASE_DIR = Path(__file__).resolve().parent
CORE_PYTHON_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "openai": "openai",
    "multipart": "python-multipart",
    "numpy": "numpy",
    "PIL": "Pillow",
    "rapidocr": "rapidocr",
    "onnxruntime": "onnxruntime",
    "pypdf": "pypdf",
    "fitz": "pymupdf",
}
CHANNEL_PYTHON_MODULES = {
    "lark_oapi": "lark-oapi",
    "Crypto": "pycryptodome",
    "qqbot_agent_sdk": "qqbot-agent-sdk",
    "qrcode": "qrcode",
}
@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str
    required: bool = True


def _read_env(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def collect_checks(include_pet: bool = False) -> list[Check]:
    checks = [
        Check("Python", sys.version_info >= (3, 10),
              f"{sys.version.split()[0]} · {sys.executable}"),
        Check("项目文件", (BASE_DIR / "gateway.py").is_file(), str(BASE_DIR)),
        Check("网页资源", (BASE_DIR / "web" / "index.html").is_file(), "web/index.html"),
    ]
    missing = [package for module, package in CORE_PYTHON_MODULES.items()
               if importlib.util.find_spec(module) is None]
    checks.append(Check(
        "核心 Python 依赖", not missing,
        "已安装" if not missing else "缺少：" + ", ".join(missing),
    ))
    missing_channels = [package for module, package in CHANNEL_PYTHON_MODULES.items()
                        if importlib.util.find_spec(module) is None]
    checks.append(Check(
        "渠道集成依赖", not missing_channels,
        "已安装" if not missing_channels else "缺少：" + ", ".join(missing_channels),
        required=False,
    ))
    env = {**_read_env(), **os.environ}
    checks.append(Check(
        "模型 API Key", bool(env.get("LLM_API_KEY", "").strip()),
        "已配置" if env.get("LLM_API_KEY", "").strip() else "请在 .env 中配置 LLM_API_KEY",
    ))
    tavily = bool(env.get("TAVILY_API_KEY", "").strip())
    checks.append(Check("联网搜索", tavily,
                        "Tavily 已配置" if tavily else "未配置 Tavily（可选）", required=False))
    if include_pet:
        node = shutil.which("node")
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        electron = BASE_DIR / "desktop_pet" / "node_modules" / "electron"
        checks.extend([
            Check("Node.js", bool(node), node or "未找到 node"),
            Check("npm", bool(npm), npm or "未找到 npm"),
            Check("桌宠依赖", electron.exists(),
                  "已安装" if electron.exists() else "需要在 desktop_pet 安装 npm 依赖"),
        ])
    return checks


def print_checks(checks: list[Check]) -> bool:
    print("\nSJTUClaw 环境检查\n")
    for check in checks:
        # Keep the launcher usable in legacy Windows consoles using GBK.
        mark = "OK" if check.ok else ("!!" if not check.required else "XX")
        print(f"  {mark} {check.label:<14} {check.detail}")
    required_ok = all(check.ok for check in checks if check.required)
    print("\n" + ("环境已就绪。" if required_ok else "存在必需项缺失，请先运行 install 或按提示配置。"))
    return required_ok


def install(include_pet: bool = True) -> int:
    print("正在安装 Python 依赖……")
    code = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(BASE_DIR / "requirements.txt")],
        cwd=BASE_DIR,
    ).returncode
    if code or not include_pet:
        return code
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        print("未找到 npm，无法安装桌宠依赖。", file=sys.stderr)
        return 1
    print("正在安装桌宠依赖……")
    return subprocess.run([npm, "install"], cwd=BASE_DIR / "desktop_pet").returncode

def health_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


def gateway_ready(base_url: str, timeout: float = 1.0) -> bool:
    try:
        with urlopen(f"{base_url}/api/health", timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def wait_for_gateway(base_url: str, process: subprocess.Popen | None, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gateway_ready(base_url):
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def start(host: str, port: int, open_browser: bool = True, pet: bool = False) -> int:
    checks = collect_checks(include_pet=pet)
    if not print_checks(checks):
        return 2
    base_url = health_url(host, port)
    gateway_process: subprocess.Popen | None = None
    pet_process: subprocess.Popen | None = None
    existing_gateway = gateway_ready(base_url)
    if existing_gateway:
        print(f"\n检测到 Gateway 已运行：{base_url}")
    else:
        print(f"\n正在启动 Gateway：{base_url}")
        gateway_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "gateway:app", "--host", host, "--port", str(port)],
            cwd=BASE_DIR,
        )
        if not wait_for_gateway(base_url, gateway_process):
            code = gateway_process.poll()
            print(f"Gateway 启动失败（退出码：{code}）。", file=sys.stderr)
            _terminate(gateway_process)
            return code or 1
    if open_browser:
        webbrowser.open(base_url)
    if pet:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        env = os.environ.copy()
        env["SJTUCLAW_GATEWAY"] = base_url
        pet_process = subprocess.Popen([npm, "start"], cwd=BASE_DIR / "desktop_pet", env=env)
    print("SJTUClaw 已启动。按 Ctrl+C 停止本次启动的服务。")
    try:
        if gateway_process is None:
            while pet_process is not None and pet_process.poll() is None:
                time.sleep(0.5)
            if pet_process is None:
                return 0
        else:
            while gateway_process.poll() is None:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nGateway 已关闭。")
    finally:
        _terminate(pet_process)
        _terminate(gateway_process)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SJTUClaw 启动与环境管理")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="检查运行环境，不修改系统")
    doctor.add_argument("--pet", action="store_true", help="同时检查桌宠环境")
    installer = sub.add_parser("install", help="安装项目依赖")
    installer.add_argument("--no-pet", action="store_true", help="不安装桌宠依赖")
    starter = sub.add_parser("start", help="启动 Gateway 并打开网页")
    starter.add_argument("--host", default="127.0.0.1")
    starter.add_argument("--port", type=int, default=8000)
    starter.add_argument("--no-browser", action="store_true")
    starter.add_argument("--pet", action="store_true", help="同时启动桌宠")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return 0 if print_checks(collect_checks(args.pet)) else 2
    if args.command == "install":
        return install(not args.no_pet)
    return start(args.host, args.port, not args.no_browser, args.pet)


if __name__ == "__main__":
    raise SystemExit(main())
