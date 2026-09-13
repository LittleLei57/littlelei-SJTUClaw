#!/usr/bin/env python3
"""项目一键自检。

默认不访问真实模型、不启动外部渠道，也不读写项目 ``data/``。
只有显式传入 ``--browser`` 时才运行真实浏览器 E2E。
"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED_MODULES = ("fastapi", "openai", "uvicorn", "multipart", "rich", "sympy")
FOCUSED_TESTS = (
    "test.test_compaction",
    "test.test_agent_loop.Step5Tests.test_agent_loop_executes_tool_then_returns_final",
    "test.test_agent_loop.Step5Tests.test_failed_install_forces_specific_native_tool_retry",
    "test.test_gateway.GatewayTests.test_health_and_web_ui",
    "test.test_scheduler.SchedulerTests.test_once_task_executes_through_runtime_and_completes",
    "test.test_workspace_tools.WorkspaceAndToolTests.test_read_pdf_attachment_without_shell",
    "test.test_streaming.StreamingTests.test_running_sse_turn_can_be_cancelled",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SJTUClaw 项目一键自检")
    parser.add_argument("--browser", action="store_true", help="额外运行真实 Chromium E2E")
    parser.add_argument("--full", action="store_true", help="运行完整 unittest 测试集")
    return parser.parse_args()


def result(label: str, ok: bool, detail: str = "") -> bool:
    icon = "通过" if ok else "失败"
    suffix = f"：{detail}" if detail else ""
    print(f"[{icon}] {label}{suffix}")
    return ok


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 240,
    cwd: Path = ROOT,
) -> tuple[bool, str]:
    effective_env = (env or os.environ).copy()
    effective_env.setdefault("PYTHONUTF8", "1")
    effective_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=effective_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"运行超时（{timeout}s）：{exc}"
    output = "\n".join(
        line
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )
    return completed.returncode == 0, output[-1600:]


def check_environment() -> bool:
    missing: list[str] = []
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    detail = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        if not missing
        else "缺少 " + ", ".join(missing)
    )
    return result("Python 环境与核心依赖", not missing, detail)


def check_gateway_smoke() -> bool:
    old_data = os.environ.get("SJTUCLAW_DATA_DIR")
    old_api_key = os.environ.get("LLM_API_KEY")
    try:
        with tempfile.TemporaryDirectory(prefix="sjtuclaw-check-") as temp:
            os.environ["SJTUCLAW_DATA_DIR"] = temp
            # A clean submission intentionally has no .env.  Runtime
            # construction only needs a syntactically non-empty key here;
            # this smoke check never sends a model request.
            os.environ.setdefault("LLM_API_KEY", "sk-self-check-placeholder")
            from bootstrap import build_runtime
            from fastapi.testclient import TestClient
            from gateway import create_app

            runtime, _ = build_runtime(temp)
            app = create_app(
                runtime,
                web_dir=ROOT / "web",
                start_background_services=False,
            )
            with TestClient(app) as client:
                health = client.get("/api/health")
                page = client.get("/")
                tools = {item["name"] for item in runtime.tool_registry.definitions()}
            expected = {
                "current_time",
                "calculate",
                "symbolic_math",
                "list_dir",
                "read_file",
                "read_attachment",
                "create_file",
                "apply_patch",
                "web_search",
                "weather_forecast",
            }
            ok = (
                health.status_code == 200
                and health.json().get("status") == "ok"
                and page.status_code == 200
                and expected.issubset(tools)
            )
            detail = f"Gateway {health.status_code} / Web {page.status_code} / Tool {len(tools)} 个"
            return result("Gateway、Web 与关键 Tool 冒烟", ok, detail)
    except Exception as exc:
        return result("Gateway、Web 与关键 Tool 冒烟", False, f"{type(exc).__name__}: {exc}")
    finally:
        if old_data is None:
            os.environ.pop("SJTUCLAW_DATA_DIR", None)
        else:
            os.environ["SJTUCLAW_DATA_DIR"] = old_data
        if old_api_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = old_api_key


def check_tests(full: bool, browser: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="sjtuclaw-check-tests-") as temp:
        env = os.environ.copy()
        env["SJTUCLAW_DATA_DIR"] = temp
        env["RUN_E2E"] = "0"
        env["LLM_API_KEY"] = "sk-self-check-placeholder"
        targets = ["discover", "-s", "test", "-q"] if full else [*FOCUSED_TESTS, "-q"]
        ok, output = run([sys.executable, "-m", "unittest", *targets], env=env, timeout=420)
    label = "完整自动化测试" if full else "Tool / Approval / Compaction / Scheduler 核心回归"
    result(label, ok, output.splitlines()[-1] if output else "无输出")
    if not ok:
        print(output)
        return False

    if browser:
        with tempfile.TemporaryDirectory(prefix="sjtuclaw-check-e2e-") as temp:
            browser_env = os.environ.copy()
            browser_env["SJTUCLAW_DATA_DIR"] = temp
            browser_env["RUN_E2E"] = "1"
            browser_env["LLM_API_KEY"] = "sk-self-check-placeholder"
            ok, output = run(
                [sys.executable, "-m", "unittest", "test.test_web_e2e", "-q"],
                env=browser_env,
                timeout=300,
            )
        if "skipped=" in output or "Ran 0 tests" in output:
            ok = False
            output += "\n请求了浏览器测试，但测试被跳过；请安装 Playwright 和 Chromium 后重试。"
        result("真实 Chromium Web E2E", ok, output.splitlines()[-1] if output else "无输出")
        if not ok:
            print(output)
            return False
    else:
        print("[跳过] 真实 Chromium Web E2E（需要时添加 --browser）")
    return True


def check_frontends() -> bool:
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node:
        return result("前端与桌宠静态检查", False, "未找到 Node.js")
    web_ok, web_output = run([node, "--check", str(ROOT / "web" / "app.js")])
    pet_ok = False
    pet_output = "未找到 npm"
    if npm:
        pet_ok, pet_output = run([npm, "run", "check"], timeout=120, cwd=ROOT / "desktop_pet")
    ok = web_ok and pet_ok
    detail = "Web JS 与桌宠脚本语法正常" if ok else (web_output + "\n" + pet_output)[-800:]
    return result("前端与桌宠静态检查", ok, detail)


def main() -> int:
    args = arguments()
    print("SJTUClaw 项目自检")
    print("=" * 56)
    checks = [
        check_environment(),
        check_gateway_smoke(),
        check_tests(args.full, args.browser),
        check_frontends(),
    ]
    print("=" * 56)
    passed = sum(checks)
    print(f"结果：{passed}/{len(checks)} 组通过")
    if not args.browser:
        print("说明：本次未运行真实浏览器 E2E；本机可用 --browser 补跑。")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
